"""Ask Google whether a venue still trades. One lookup, one cache, one rule.

Untappd's venue database is append-only in practice: a bar that shut in 2019
keeps its page, its history and its stats, and since `run` ranks on those
stats a long-dead venue outranks a good one that opened last year. #7 is that
problem. `classify.looks_closed` is its free first tier -- substantial history,
no monthly check-ins -- and is suggestive rather than conclusive, because a
quiet neighbourhood bar looks the same and a seasonal one looks identical.

This is the authoritative second tier: Places `businessStatus`.

**The rule, and it is the whole design.** *A missing Places match must not
imply closure.* A failed lookup collapses cases that want opposite outcomes --
the place closed, the place was renamed, the place is too new, Places simply
lacks it. So only an explicit `CLOSED_PERMANENTLY` or `CLOSED_TEMPORARILY`
marks a venue closed. Everything else -- no match, a timeout, a status Google
adds next year -- leaves it unknown and visible.

The cost of that is stated plainly: a venue that quietly shut and was delisted
survives as unknown rather than being caught. That is the right trade. A false
closure silently deletes a real bar from the map, which is the failure the
quality gate and the fail-closed guardrails exist to prevent.

Shape follows `geocode.py`, which already had the parts worth copying: opt-in
behind its own env var, an on-disk cache so a re-run is not re-billed, and a
hard split between "this venue has no match" (per-venue, fine) and "the
integration is broken" (abort, loudly). That split is `GeocoderUnavailable`'s
whole reason for existing and `PlacesUnavailable` is the same idea -- see the
note on `resolve_closures` for why the ordering of its `except` clauses is
load-bearing rather than stylistic.

A separate key from `GOOGLE_GEOCODING_KEY`: different SKU, and somebody may
reasonably want geocoding without sending addresses for classification.

Cost note, current as of September 2026. `businessStatus` is a **Pro** field on
Text Search, so a request carrying it bills the Places API Text Search Pro SKU:
5,000 events/month free, then $25.60/1000. `id`, `displayName` and `types` are
Essentials (IDs Only) fields and ride along at no extra charge, which is why
they are fetched and cached here despite nothing consuming them yet -- #20 asks
for one lookup, one cache and one set of failure semantics to serve #6, #7 and
#8, and fetching `types` now is what stops #6 and #8 needing a second call
later. At ~100 venues a week, cached, this is inside the free allowance -- but
billing must be enabled on the key regardless. Per-SKU free tiers replaced the
old shared $200 credit in March 2025.

Privacy, named rather than inherited: this sends venue names and addresses to
Google. Same shape as the existing geocoding path, and `SECURITY.md` says so.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from .config import STATE_DIR, Settings
from .models import Venue

log = logging.getLogger(__name__)


class PlacesUnavailable(RuntimeError):
    """Places itself is not working -- key, quota, billing or network.

    Distinct from a venue that simply has no match, exactly as
    `GeocoderUnavailable` is. Collapsing the two is how a rejected API key
    ships a CSV in which every venue reads `unmatched` and the envelope still
    says `ok: true`.
    """


PLACES_CACHE = STATE_DIR / "places_cache.json"
SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

# Requested together, billed as one Text Search Pro event. `businessStatus` is
# the field this module exists for; the rest are free and are here for #6/#8.
FIELD_MASK = ",".join((
    "places.id",
    "places.displayName",
    "places.types",
    "places.businessStatus",
))

# Not a `businessStatus` value. The two states a venue can be in before Google
# has said anything, kept distinct because they mean different things to a
# reader: nobody asked, versus asked and Google does not know this place.
UNCHECKED = ""
UNMATCHED = "unmatched"

# The only values that close a venue. An allow-list rather than a deny-list:
# Google has already added `FUTURE_OPENING` once, and a deny-list would have
# read a not-yet-open venue as a shut one.
CLOSED_STATUSES = frozenset({"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"})

# Anything Google answers with is cached; the request that produced no answer
# is not. A venue Places did not know last week may be listed this week, and
# caching the negative would quietly make "not found once" permanent.
_CACHEABLE = True


@dataclass(frozen=True)
class PlaceMatch:
    """One Place, reduced to the fields this project pays for."""

    place_id: str
    display_name: str
    business_status: str | None
    types: tuple[str, ...]

    def to_cache(self) -> dict:
        return {
            "place_id": self.place_id,
            "display_name": self.display_name,
            "business_status": self.business_status,
            "types": list(self.types),
        }

    @staticmethod
    def from_cache(raw: dict) -> PlaceMatch:
        return PlaceMatch(
            place_id=raw.get("place_id", ""),
            display_name=raw.get("display_name", ""),
            business_status=raw.get("business_status"),
            types=tuple(raw.get("types", ())),
        )


def _load_cache() -> dict[str, dict]:
    """A corrupt cache costs money to rebuild. It must not stop the run.

    Deliberately different from the guardrail files, which fail *closed* on a
    bad read because their job is to refuse. This one's job is to save
    requests, so the safe direction is to start empty, say so loudly, and
    re-bill once -- rather than crash a run over a half-written JSON file.
    """
    if not PLACES_CACHE.exists():
        return {}
    try:
        cached = json.loads(PLACES_CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("places cache at %s is unreadable (%s) -- starting empty. "
                    "This run will re-bill for venues already resolved.",
                    PLACES_CACHE, exc)
        return {}
    return cached if isinstance(cached, dict) else {}


def _save_cache(cache: dict[str, dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PLACES_CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")


def _query_for(v: Venue) -> str:
    """The same name/address/city join `geocode` uses, for the same reason."""
    parts = [v.ref.name, v.ref.address, v.ref.city]
    return ", ".join(p for p in parts if p)


def _search(client, key: str, query: str) -> PlaceMatch | None:
    """One Text Search call. `None` means no match -- never "closed".

    Status codes are read rather than left to `raise_for_status`, because the
    difference between 403 and a transport blip is the difference between
    stopping the run and skipping one venue.
    """
    response = client.post(
        SEARCH_TEXT_URL,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": FIELD_MASK,
        },
        json={"textQuery": query, "pageSize": 1},
    )

    if response.status_code in (401, 403, 429):
        # Rejected key, Places API not enabled on the project, billing off, or
        # quota exhausted. Every one of those will fail identically for the
        # next hundred venues, so none of them is a per-venue fact.
        detail = ""
        try:
            detail = response.json().get("error", {}).get("message", "")
        except Exception:  # a non-JSON error body is still a broken key
            detail = response.text[:200]
        raise PlacesUnavailable(
            f"Places Text Search returned HTTP {response.status_code}: {detail}"
        )
    if response.status_code >= 400:
        # 5xx and the rest: could genuinely be this one request.
        raise RuntimeError(f"Places Text Search HTTP {response.status_code}")

    results = response.json().get("places") or []
    if not results:
        return None
    top = results[0]
    return PlaceMatch(
        place_id=top.get("id", ""),
        display_name=(top.get("displayName") or {}).get("text", ""),
        business_status=top.get("businessStatus"),
        types=tuple(top.get("types", ())),
    )


def resolve_closures(venues: list[Venue], s: Settings) -> list[Venue]:
    """Return a NEW list with `business_status` filled in where Places answered.

    No key is a no-op, not a guess: every venue comes back `UNCHECKED` and
    nothing is condemned. That mirrors geocoding falling back rather than
    inventing coordinates.

    The ordering of the `except` clauses below is the load-bearing part of this
    function. `PlacesUnavailable` is re-raised *before* the broad handler can
    see it, because this repo has now written that raise five times and had a
    broad `except` two frames up swallow it four of them. A test asserts it
    from this layer rather than from `_search`, since `_search` raising was
    never the part that broke.
    """
    if not s.google_places_key:
        log.info("No GOOGLE_PLACES_KEY set -- closure check skipped, "
                 "%d venue(s) left unchecked.", len(venues))
        return list(venues)

    cache = _load_cache()
    resolved: dict[str, str] = {}
    attempted = errored = 0

    try:
        with httpx.Client(timeout=s.request_timeout_s,
                          headers={"User-Agent": s.user_agent}) as client:
            for v in venues:
                query = _query_for(v)
                if not query:
                    continue
                if query in cache:
                    resolved[v.ref.venue_id] = _status_of(
                        PlaceMatch.from_cache(cache[query]))
                    continue
                attempted += 1
                try:
                    match = _search(client, s.google_places_key, query)
                except PlacesUnavailable:
                    # Never per-venue: the next hundred lookups fail the same
                    # way. Stop, rather than writing `unmatched` a hundred
                    # times and calling it a result.
                    raise
                except Exception as exc:
                    # This one request. The venue stays UNCHECKED -- not
                    # UNMATCHED, which would claim Google was asked and
                    # answered.
                    log.error("places lookup failed for %r: %s", query, exc)
                    errored += 1
                    continue
                if match is None:
                    # Asked, and Google does not list it. Still not a closure.
                    log.info("no Places match for %r", query)
                    resolved[v.ref.venue_id] = UNMATCHED
                    continue
                if _CACHEABLE:
                    cache[query] = match.to_cache()
                resolved[v.ref.venue_id] = _status_of(match)
    finally:
        # Written even when the loop aborts. Everything above this point has
        # already been billed, and throwing it away means paying for it twice
        # -- which is exactly what a rejected key halfway through a corpus
        # would otherwise cost.
        _save_cache(cache)

    # Every lookup erroring is not a hundred unlucky venues; it is Places
    # being unreachable or blocked. A venue with no match does not reach this
    # count -- that path returns None rather than raising -- so this cannot
    # fire on a genuinely obscure batch. Same guard, same reasoning, as
    # `geocode_missing`.
    if attempted and errored == attempted:
        raise PlacesUnavailable(
            f"All {attempted} Places lookup(s) failed. Places is unreachable, "
            f"blocked or misconfigured."
        )

    return [
        v.with_business_status(resolved[v.ref.venue_id])
        if v.ref.venue_id in resolved else v
        for v in venues
    ]


def _status_of(match: PlaceMatch) -> str:
    """A match with no `businessStatus` is matched but unstated, not open."""
    return match.business_status or UNMATCHED


def counts(venues: list[Venue]) -> dict[str, int]:
    """How the corpus came back, by status. What the envelope reports."""
    tally: dict[str, int] = {}
    for v in venues:
        key = v.business_status or UNCHECKED
        tally[key or "unchecked"] = tally.get(key or "unchecked", 0) + 1
    return dict(sorted(tally.items()))
