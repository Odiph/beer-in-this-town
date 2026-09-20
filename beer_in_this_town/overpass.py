"""Ask OpenStreetMap what drinking places are within N km of a point.

This is the geographic half that Untappd's own search cannot do. `/search`
matches venue *names*: `q="Tel Aviv"` finds `Tel Aviv Grill` in California and
misses `Lauter`, two streets from the centre, because a bar has no reason to
carry its city in its name. Measured 2026-09-20 against a real corpus -- the
`q="Tel Aviv"` sample was train stations, hotels, a pita place and a light
rail platform, while one Overpass query returned **258 named drinking venues
within 15 km, every one with coordinates**.

It fills the same slot as `places.nearby_venue_names` and is preferred there:
no key, no billing, no quota, and it keeps Google out of the *collection*
path. That matters beyond cost -- making Places a dependency of collection
rather than of optional enrichment was a change in posture that would have had
to be disclosed in the README. Now it does not happen.

**What this does not solve.** OSM names are OSM's names, and Untappd stats
need an Untappd `venue_id`, so a name still has to be resolved by search.
Measured: roughly 40% of OSM names find any Israeli venue at all, and that is
a ceiling rather than an accuracy -- `Geula` matched `geula-suites`, a hotel.
The attrition is also *biased*, not random: distinctive names (`פורט סעיד`,
`המעוז`) resolve exactly, generic ones (`Plaza`, `Friends`, `murphy's`) drown
in twenty worldwide matches. Anything consuming these names must expect to
lose the generically-named bars, and must gate the join on coordinates --
which is why `OsmVenue` carries them and why elements without them are
dropped.

Two rules hold this module up:

1. **A broken integration is not an empty city.** Every transport or HTTP
   failure raises `OverpassUnavailable`. `[]` is returned only when OSM
   genuinely maps nothing there, which is a real answer about a real place.
   This is `GeocoderUnavailable` and `PlacesUnavailable`'s split, for the same
   reason: collapsing the two is how a throttled endpoint ships a corpus of
   nothing while the envelope still says `ok: true`.
2. **Nearest first.** OSM has no prominence to rank by, so a limit that
   truncated an arbitrary order would quietly change which city was asked
   about. Sorting by distance makes truncation lose the far edge, which is
   the only defensible thing to lose.

Licence: OSM data is ODbL. `ATTRIBUTION` lives here, next to the fetch, so an
export that carries this data can carry its obligation too.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass

import httpx

from .config import STATE_DIR, Settings
from .geo import haversine_km

log = logging.getLogger(__name__)

ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"

# Overpass is volunteer-run and its usage policy asks callers to cache. It is
# not a cost saving here, it is load: the first live run of this module hit
# 504 on its *second* request for data it had already fetched seconds
# earlier, because `nearby_venue_names` re-asked. One answer, one request.
OVERPASS_CACHE = STATE_DIR / "overpass_cache.json"

# Server-side budget, stated inside the query. Overpass enforces its own; ours
# being explicit is what makes a slow answer a refusal rather than a hang.
QUERY_TIMEOUT_S = 90

# Identify honestly. The Chrome user-agent `http_client` sends exists to look
# like the browser whose session it carries -- on untappd.com that is
# consistency, here it would be a lie told to a volunteer-run endpoint whose
# usage policy asks who is calling.
USER_AGENT = "beer-in-this-town/0.1.0 (+https://github.com/Odiph/beer-in-this-town)"

# What counts as a drinking place. `shop=alcohol` earns its place: a bottle
# shop with Untappd check-ins is exactly the kind of venue `q="<city>"` never
# finds, and the existing corpus already carries category "Beer Store".
_SELECTORS = (
    'nwr["amenity"~"^(bar|pub|biergarten)$"]',
    'nwr["craft"~"^(brewery|distillery)$"]',
    'nwr["microbrewery"="yes"]',
    'nwr["shop"="alcohol"]',
)

# Order matters only for reporting: the first tag present names the kind.
_KIND_TAGS = ("amenity", "craft", "shop")


class OverpassUnavailable(RuntimeError):
    """Overpass itself is not working -- throttled, down, or unreachable.

    Distinct from "OSM maps no bars here", exactly as `PlacesUnavailable` is
    distinct from "this venue has no match". A caller that cannot tell them
    apart will report a rate-limited endpoint as an empty city.
    """


@dataclass(frozen=True)
class OsmVenue:
    """A drinking place OSM knows about, with the coordinates that make the
    later join to Untappd checkable rather than hopeful."""

    name: str
    lat: float
    lng: float
    kind: str


def _load_cache() -> dict[str, dict]:
    if OVERPASS_CACHE.exists():
        try:
            return json.loads(OVERPASS_CACHE.read_text(encoding="utf-8"))
        except ValueError:
            # A half-written cache is not worth an abort; it is worth a
            # refetch. Losing a cache costs one request.
            log.warning("Overpass cache was unreadable; refetching.")
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OVERPASS_CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")


def _cache_key(url: str, query: str) -> str:
    """The endpoint is part of the key: two mirrors can disagree, and a
    cached answer from one must not be served as the other's."""
    return hashlib.sha256(f"{url}\n{query}".encode()).hexdigest()[:32]


def _build_query(centre: tuple[float, float], radius_km: float) -> str:
    lat, lng = centre
    around = f"(around:{radius_km * 1000:.0f},{lat},{lng})"
    body = "\n  ".join(f"{sel}{around};" for sel in _SELECTORS)
    return f"[out:json][timeout:{QUERY_TIMEOUT_S}];\n(\n  {body}\n);\nout center tags;"


def _coords(element: dict) -> tuple[float, float] | None:
    """A node carries lat/lon; a way or relation carries `center` because the
    query asked for `out center`. Anything else cannot be placed."""
    if element.get("lat") is not None and element.get("lon") is not None:
        return float(element["lat"]), float(element["lon"])
    centre = element.get("center") or {}
    if centre.get("lat") is not None and centre.get("lon") is not None:
        return float(centre["lat"]), float(centre["lon"])
    return None


def _kind(tags: dict) -> str:
    for tag in _KIND_TAGS:
        value = tags.get(tag)
        if value:
            return str(value)
    return "unknown"


def nearby_venues(centre: tuple[float, float], radius_km: float,
                  s: Settings) -> list[OsmVenue]:
    """Drinking places OSM maps within `radius_km` of `centre`, nearest first.

    No default radius, deliberately, for the same reason there is no default
    city: it would answer a question only the caller can answer, and answer it
    about somewhere nobody named.

    An empty list is a real answer. A failure is an exception.
    """
    query = _build_query(centre, radius_km)

    cache = _load_cache()
    key = _cache_key(s.overpass_url, query)
    entry = cache.get(key)
    if entry and (time.time() - entry.get("fetched_at", 0)) < s.cache_ttl_s:
        log.info("Overpass: cached answer, no request.")
        return _venues_from(entry.get("elements", []), centre)

    try:
        with httpx.Client(timeout=QUERY_TIMEOUT_S + 10.0) as client:
            response = client.post(
                s.overpass_url,
                content=query.encode("utf-8"),
                headers={"User-Agent": USER_AGENT,
                         "Content-Type": "text/plain; charset=utf-8"},
            )
    except httpx.HTTPError as exc:
        raise OverpassUnavailable(
            f"Could not reach Overpass at {s.overpass_url}: {exc}") from exc

    if response.status_code != 200:
        # 429 and 504 are Overpass saying "too busy", which is its normal way
        # of shedding load. Retrying harder is exactly wrong; the caller is
        # told so it can back off or pick another endpoint.
        raise OverpassUnavailable(
            f"Overpass returned HTTP {response.status_code}: "
            f"{response.text[:200]}")

    try:
        payload = response.json()
    except ValueError as exc:
        # A maintenance page is HTTP 200 with HTML in it. Parsed as "no
        # elements" that would read as a city with no bars.
        raise OverpassUnavailable(
            f"Overpass returned a body that is not JSON: "
            f"{response.text[:200]}") from exc

    elements = payload.get("elements", [])
    # Cached only after a clean parse. A 504 or a maintenance page must never
    # become a cached "no bars here" that survives for twelve hours.
    cache[key] = {"fetched_at": time.time(), "elements": elements}
    _save_cache(cache)

    return _venues_from(elements, centre, radius_km)


def _venues_from(elements: list[dict], centre: tuple[float, float],
                 radius_km: float | None = None) -> list[OsmVenue]:
    venues: list[OsmVenue] = []
    unplaceable = 0
    for element in elements:
        tags = element.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        point = _coords(element)
        if point is None:
            unplaceable += 1
            continue
        venues.append(OsmVenue(name=name, lat=point[0], lng=point[1],
                               kind=_kind(tags)))

    if unplaceable:
        log.warning("%d named OSM element(s) had no coordinates and were "
                    "dropped -- they cannot be placed or matched.", unplaceable)

    venues.sort(key=lambda v: haversine_km(centre, (v.lat, v.lng)))
    if radius_km is not None:
        log.info("Overpass: %d named drinking venue(s) within %.1f km.",
                 len(venues), radius_km)
    return venues


def nearby_venue_names(centre: tuple[float, float], radius_km: float,
                       s: Settings, limit: int = 60) -> list[str]:
    """Names of drinking places near a point, nearest first, deduplicated.

    Drop-in for `places.nearby_venue_names`: same shape, same slot, no key.

    Deduplicated because these become *search terms* and the same string
    searched twice is a wasted request -- while `nearby_venues` keeps both,
    since two bars really can share a name and they are two different places.
    """
    names: list[str] = []
    seen: set[str] = set()
    for venue in nearby_venues(centre, radius_km, s):
        key = venue.name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(venue.name)
        if len(names) >= limit:
            break
    return names
