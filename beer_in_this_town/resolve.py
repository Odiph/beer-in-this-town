"""Join map-swept names to their Untappd venue pages.

The app sweep is the geographic census: it knows a venue's name and roughly
where it is. The venue page knows the rest -- the id, the check-in counts, and
coordinates the venue published itself, which beat any fit of a map pin.

**The join is by name, and names are the weak part.** The app shows them
bilingual (`Mike's Place (מייקס פלייס)`), some are generic everywhere
(`Beer Garden`), and chains put near-identical names a kilometre apart. So a
name match is only a *candidate*. It is accepted when the page's own
coordinates land within `MAX_MATCH_M` of where the map put the pin, and
refused otherwise. Without that check the failure is silent: a Berlin bar's
check-ins attached to a Tel Aviv pin, in a corpus whose every row looks
filled in.

Anything that does not resolve stays in the output as the map venue, with no
counts -- unknown, never zero, never dropped. Every outcome is recorded with
its reason, so a thin join reads as a thin join rather than a small city.

Search and fetch are injected, so this module touches no browser and no
network; the caller decides how either is paced.
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .app_export import to_venues
from .app_sweep import SweepResult
from .app_sweep import Venue as SweptVenue
from .http_client import BudgetExceeded, RateLimitTripped, TransportUnavailable
from .models import Venue, VenueRef

log = logging.getLogger(__name__)

# How similar two names must be to count as a candidate. High on purpose:
# `Beer Bazaar Habima` and `Beer Bazaar Carmel` are different bars.
NAME_MIN = 0.85

# How far the venue page's coordinates may sit from the map pin. The
# calibrated map fit is ~18 m median, an uncalibrated one ~600 m; this admits
# both and still refuses the same name in the next town.
MAX_MATCH_M = 1000.0

# Venue pages fetched per swept name at most. Each is a real request against
# the paced budget, and a name with five far-away namesakes should not spend
# five of them.
MAX_FETCHES_PER_NAME = 2

# Deliberate stops, not per-venue failures. Same rule as `fetch_venues`:
# swallowing one keeps firing a request per remaining venue into an active
# rate limit, which is exactly what the client exists to prevent.
_STOPS = (RateLimitTripped, BudgetExceeded, TransportUnavailable)

# The app's bilingual suffix, `Name (שם)`, and `Name - Branch`.
_SECONDARY = re.compile(r"\s*(\(.*|\s-\s.*|\s–\s.*)$")


def _norm(s: str) -> str:
    # Every script survives. Stripping to ASCII once collapsed all Hebrew
    # names to "" and matched them to each other.
    s = unicodedata.normalize("NFKC", s).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def _variants(name: str) -> set[str]:
    out = {_norm(name), _norm(_SECONDARY.sub("", name))}
    return {v for v in out if v}


def name_score(a: str, b: str) -> float:
    """How alike two venue names are, 0 to 1, forgiving the app's suffixes."""
    return max((SequenceMatcher(None, x, y).ratio()
                for x in _variants(a) for y in _variants(b)), default=0.0)


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class Resolution:
    """What happened to one swept name, and why."""

    name: str
    status: str  # resolved|too_far|no_match|unverified|unlocated|
    #              fetch_failed|search_failed|duplicate
    venue: Venue | None = None
    detail: str = ""


@dataclass
class Report:
    resolutions: list[Resolution] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return dict(Counter(r.status for r in self.resolutions))


def resolve_one(swept: SweptVenue,
                search: Callable[[str], list[VenueRef]],
                fetch: Callable[[VenueRef], Venue]) -> Resolution:
    """Find `swept`'s venue page, or say precisely why not."""
    if swept.lat is None or swept.lng is None:
        return Resolution(swept.name, "unlocated",
                          detail="no map position to check a match against")
    here = (swept.lat, swept.lng)

    try:
        refs = search(swept.name)
    except _STOPS:
        raise
    except Exception as exc:  # one failed search must not end the run
        return Resolution(swept.name, "search_failed", detail=str(exc)[:200])

    ranked = sorted(((name_score(swept.name, r.name), r) for r in refs),
                    key=lambda p: -p[0])
    candidates = [r for score, r in ranked if score >= NAME_MIN]
    if not candidates:
        best = f"best was {ranked[0][1].name!r}" if ranked else "no results"
        return Resolution(swept.name, "no_match", detail=best)

    last = Resolution(swept.name, "no_match")
    for ref in candidates[:MAX_FETCHES_PER_NAME]:
        try:
            page = fetch(ref)
        except _STOPS:
            raise
        except Exception as exc:
            last = Resolution(swept.name, "fetch_failed",
                              detail=f"{ref.url}: {str(exc)[:160]}")
            continue
        if page.lat is None or page.lng is None:
            last = Resolution(swept.name, "unverified",
                              detail=f"{ref.url} publishes no coordinates")
            continue
        gap = _metres(here, (page.lat, page.lng))
        if gap <= MAX_MATCH_M:
            return Resolution(swept.name, "resolved", venue=page,
                              detail=f"{gap:.0f} m from the pin")
        last = Resolution(swept.name, "too_far",
                          detail=f"{ref.url} is {gap / 1000:.1f} km away")
    return last


def enrich(swept: list[SweptVenue], city: str,
           search: Callable[[str], list[VenueRef]],
           fetch: Callable[[VenueRef], Venue],
           progress: Callable[[int, int, str], None] | None = None,
           ) -> tuple[list[Venue], Report]:
    """Every swept venue, with its page's data wherever the join holds.

    Order is kept. Two swept names landing on one page are one venue -- the
    app lists `Oscar Wilde` and `Oscar Wilde - Irish Pub` separately -- so
    the second is recorded as a duplicate and not repeated.
    """
    report = Report()
    out: list[Venue] = []
    seen_ids: set[str] = set()
    for i, sv in enumerate(swept, 1):
        if progress:
            progress(i, len(swept), sv.name)
        res = resolve_one(sv, search, fetch)
        if res.venue is not None and res.venue.ref.venue_id in seen_ids:
            res = Resolution(sv.name, "duplicate", detail=res.venue.ref.url)
        report.resolutions.append(res)
        log.info("%-9s %s  %s", res.status, sv.name, res.detail)

        if res.status == "duplicate":
            continue
        if res.venue is not None:
            seen_ids.add(res.venue.ref.venue_id)
            out.append(res.venue)
        else:
            out.extend(to_venues(SweepResult(venues=[sv]), city))
    return out, report
