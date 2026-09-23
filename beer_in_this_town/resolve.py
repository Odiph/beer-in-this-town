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

**Namesakes are ranked by place before any page is fetched.** The first live
run (Tel Aviv, 33 names) lost `Mike's Place`, `Django` and `Oscar Wilde` this
way: the name search returned far-away namesakes first, they filled the
two-fetch cap, and the local venue was never looked at. The search card
carries a location line, so candidates whose card names the city go first,
cards with no location next, and cards naming another place last. When no
card names the city, one city-qualified search (`Mike's Place Tel Aviv`) is
tried before any fetch is spent.

Anything that does not resolve stays in the output as the map venue, with no
counts -- unknown, never zero, never dropped. Every outcome is recorded with
its reason, so a thin join reads as a thin join rather than a small city.

**This is a per-name lookup, not the removed city search.** Searching
Untappd for a *city* matched venue names, not places, and v0.2 removed it.
Looking up one *name* the map already placed is how a venue page is found at
all. What that lookup needs (the search URL, the card parser and the
browser session) lives here rather than in the old search module, so it
survives that module's removal.

`resolve_one` and `enrich_rows` take search and fetch as callables and touch
no browser and no network; `BrowserNameSearch` and `page_fetcher` are the
real ones the `enrich` command plugs in.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

from . import config
from .app_export import to_venues
from .app_sweep import SweepResult
from .app_sweep import Venue as SweptVenue
from .http_client import (
    BudgetExceeded,
    PoliteClient,
    RateLimitTripped,
    ReadBudget,
    TransportUnavailable,
)
from .models import Venue, VenueRef
from .parsers import StatsLoginRequired

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
# A login-gated venue page is a stop too: signed out, nearly every page is
# gated, and carrying on spends the request budget on pages with no stats.
_STOPS = (RateLimitTripped, BudgetExceeded, TransportUnavailable,
          StatsLoginRequired)

# The app's bilingual suffix, `Name (שם)`, and `Name - Branch`.
_SECONDARY = re.compile(r"\s*(\(.*|\s-\s.*|\s–\s.*)$")

# How a card's location line relates to the city being enriched.
IN_CITY, UNKNOWN_PLACE, ELSEWHERE = 2, 1, 0

# `outside`: a search-sweep row whose page places it outside the city (a
# namesake: "london" also finds New London, CT). Dropped, like a duplicate.
STATUSES = ("resolved", "too_far", "no_match", "unverified", "unlocated",
            "fetch_failed", "search_failed", "duplicate", "outside")


class Located(Protocol):
    """Anything with a name and a map position: a swept venue or a CSV row."""

    name: str
    lat: float | None
    lng: float | None


def _norm(s: str) -> str:
    # Every script survives. Stripping to ASCII once collapsed all Hebrew
    # names to "" and matched them to each other.
    s = unicodedata.normalize("NFKC", s).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def _variants(name: str) -> set[str]:
    out = {_norm(name), _norm(_SECONDARY.sub("", name))}
    return {v for v in out if v}


def base_name(name: str) -> str:
    """`Mike's Place (מייקס פלייס)` -> `Mike's Place`: what to search for."""
    return _SECONDARY.sub("", name).strip() or name.strip()


def name_score(a: str, b: str) -> float:
    """How alike two venue names are, 0 to 1, forgiving the app's suffixes."""
    return max((SequenceMatcher(None, x, y).ratio()
                for x in _variants(a) for y in _variants(b)), default=0.0)


def place_rank(ref: VenueRef, city: str | None) -> int:
    """Does this search card say it is in `city`?

    The card's location line (`Tel Aviv, תל אביב, ישראל`) is read, with the
    address as a fallback because a sparse card can file its only location
    there. A card naming somewhere else is not refused -- `Jaffa` is in Tel
    Aviv and the page's coordinates are the real judge -- it is only tried
    last, so it cannot spend the fetch cap before a local candidate does.
    """
    wanted = _norm(city or "")
    said = " ".join(p for p in (ref.city, ref.address) if p)
    if not wanted:
        return UNKNOWN_PLACE
    if not said:
        return UNKNOWN_PLACE
    said_n = _norm(said)
    head = _norm(said.split(",")[0])
    if wanted in said_n or (head and head in wanted):
        return IN_CITY
    return ELSEWHERE


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class Resolution:
    """What happened to one swept name, and why."""

    name: str
    status: str  # one of STATUSES
    venue: Venue | None = None
    detail: str = ""


@dataclass
class Report:
    resolutions: list[Resolution] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return dict(Counter(r.status for r in self.resolutions))


def _rank(name: str, refs: list[VenueRef], city: str | None
          ) -> list[VenueRef]:
    """Name-qualified candidates, local first, best name first within."""
    scored = [(name_score(name, r.name), place_rank(r, city), r) for r in refs]
    good = [(s, p, r) for s, p, r in scored if s >= NAME_MIN]
    good.sort(key=lambda t: (-t[1], -t[0]))
    return [r for _, _, r in good]


def _search(search: Callable[[str], list[VenueRef]], query: str
            ) -> list[VenueRef] | Resolution:
    try:
        return search(query)
    except _STOPS as exc:
        log.error("Stopping enrich at the search for %r: %s", query, exc)
        raise
    except Exception as exc:  # one failed search must not end the run
        return Resolution(query, "search_failed", detail=str(exc)[:200])


def _candidates(swept: Located, search: Callable[[str], list[VenueRef]],
                city: str | None) -> list[VenueRef] | Resolution:
    """Search the name; add one city-qualified search if nothing is local."""
    first = _search(search, swept.name)
    if isinstance(first, Resolution):
        return Resolution(swept.name, first.status, detail=first.detail)
    refs = list(first)
    ranked = _rank(swept.name, refs, city)
    if city and not any(place_rank(r, city) == IN_CITY for r in ranked):
        qualified = f"{base_name(swept.name)} {city}"
        more = _search(search, qualified)
        if isinstance(more, list):
            seen = {r.venue_id for r in refs}
            refs += [r for r in more if r.venue_id not in seen]
            ranked = _rank(swept.name, refs, city)
    if not ranked:
        scored = sorted(refs, key=lambda r: -name_score(swept.name, r.name))
        best = f"best was {scored[0].name!r}" if scored else "no results"
        return Resolution(swept.name, "no_match", detail=best)
    return ranked


def resolve_one(swept: Located,
                search: Callable[[str], list[VenueRef]],
                fetch: Callable[[VenueRef], Venue],
                city: str | None = None) -> Resolution:
    """Find `swept`'s venue page, or say precisely why not.

    `city`, when given, ranks namesakes by the search card's location line
    and allows one city-qualified search; see the module docstring.
    """
    if swept.lat is None or swept.lng is None:
        return Resolution(swept.name, "unlocated",
                          detail="no map position to check a match against")
    here = (swept.lat, swept.lng)

    candidates = _candidates(swept, search, city)
    if isinstance(candidates, Resolution):
        return candidates

    last = Resolution(swept.name, "no_match")
    for ref in candidates[:MAX_FETCHES_PER_NAME]:
        try:
            page = fetch(ref)
        except _STOPS as exc:
            log.error("Stopping enrich at %s: %s", ref.url, exc)
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


def _as_map_venue(sv: Located, city: str) -> Venue:
    """The row an unresolved venue keeps: its map data, counts unknown."""
    if isinstance(sv, Venue):
        return sv
    if isinstance(sv, SweptVenue):
        return to_venues(SweepResult(venues=[sv]), city)[0]
    return to_venues(SweepResult(venues=[SweptVenue(sv.name, 0, 0, sv.lat,
                                                    sv.lng)]), city)[0]


@dataclass(frozen=True)
class _Named:
    """Adapts a CSV `Venue` (name under `.ref`) to the `Located` shape."""

    name: str
    lat: float | None
    lng: float | None


def _known_page(raw: object) -> VenueRef | None:
    """A search-sweep row: a venue page with an id and no map pin."""
    if isinstance(raw, Venue) and raw.ref.url and not raw.has_coords:
        return raw.ref
    return None


def resolve_by_id(ref: VenueRef, fetch: Callable[[VenueRef], Venue],
                  within: Callable[[float, float], bool] | None = None
                  ) -> Resolution:
    """Fetch a venue page the search already identified. Nothing is guessed.

    The page is the venue, so a page without coordinates still resolves --
    with its stats, unplaced. `within` is the city's bounds; a page outside
    them is a namesake the text search matched, and is reported `outside`.
    """
    try:
        page = fetch(ref)
    except _STOPS as exc:
        log.error("Stopping enrich at %s: %s", ref.url, exc)
        raise
    except Exception as exc:
        return Resolution(ref.name, "fetch_failed",
                          detail=f"{ref.url}: {str(exc)[:160]}")
    if page.lat is None or page.lng is None:
        return Resolution(ref.name, "resolved", venue=page,
                          detail="by id; the page publishes no coordinates")
    if within is not None and not within(page.lat, page.lng):
        return Resolution(ref.name, "outside",
                          detail=f"{ref.url} is at {page.lat:.2f}, "
                                 f"{page.lng:.2f}, outside the city")
    return Resolution(ref.name, "resolved", venue=page, detail="by id")


def _located(sv: Located | Venue) -> Located:
    if isinstance(sv, Venue):
        return _Named(sv.ref.name, sv.lat, sv.lng)
    return sv


class ReadingRhythm:
    """Pause the way a person looking venues up would, between venues.

    The per-request gap (2-4.5s) paces requests. It says nothing about the
    run: a whole city at one page every four seconds, for hours, is the
    shape of a script. So between venues there is a longer jittered pause,
    and every 20-40 venues a break of a few minutes. It only ever slows the
    run down -- the per-request floors and the hourly ceiling still apply.
    """

    VENUE_GAP_S = (6.0, 15.0)
    BREAK_EVERY = (20, 40)
    BREAK_S = (120.0, 360.0)

    def __init__(self, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None,
                 requests_used: Callable[[], int] | None = None) -> None:
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._until_break = self._rng.randint(*self.BREAK_EVERY)
        # A venue answered wholly from cache made no request, so there is
        # nothing to pace. Without this a resumed run re-waited ~10 s per
        # cached venue -- 25 minutes before reaching new work, after a
        # London run was stopped at venue 145.
        self._used = requests_used
        self._last_used = requests_used() if requests_used else None

    def __call__(self, done: int) -> None:
        if self._used is not None:
            now = self._used()
            fetched, self._last_used = now != self._last_used, now
            if not fetched:
                return
        self._until_break -= 1
        if self._until_break <= 0:
            pause = self._rng.uniform(*self.BREAK_S)
            log.info("Taking a %.0fs break after %d venue(s).", pause, done)
            self._until_break = self._rng.randint(*self.BREAK_EVERY)
        else:
            pause = self._rng.uniform(*self.VENUE_GAP_S)
        self._sleep(pause)


def enrich_rows(swept: list, city: str,
                search: Callable[[str], list[VenueRef]],
                fetch: Callable[[VenueRef], Venue],
                progress: Callable[[int, int, str], None] | None = None,
                rest: Callable[[int], None] | None = None,
                within: Callable[[float, float], bool] | None = None,
                ) -> tuple[list[tuple[Venue, Resolution]], Report]:
    """Every swept venue paired with how it resolved, duplicates merged.

    `swept` holds sweep `Venue`s from `app_sweep` or rows read back from a
    stage CSV (`models.Venue`); both work. Order is kept. Two swept names
    landing on one page are one venue -- the app lists `Oscar Wilde` and
    `Oscar Wilde - Irish Pub` separately -- so the second is recorded as a
    duplicate and not repeated.
    """
    report = Report()
    out: list[tuple[Venue, Resolution]] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(swept, 1):
        sv = _located(raw)
        if progress:
            progress(i, len(swept), sv.name)
        known = _known_page(raw)
        res = (resolve_by_id(known, fetch, within) if known is not None
               else resolve_one(sv, search, fetch, city))
        if rest is not None and i < len(swept):
            rest(i)
        if res.venue is not None and res.venue.ref.venue_id in seen_ids:
            res = Resolution(sv.name, "duplicate", detail=res.venue.ref.url)
        report.resolutions.append(res)
        log.info("%-9s %s  %s", res.status, sv.name, res.detail)

        if res.status in ("duplicate", "outside"):
            continue
        if res.venue is not None:
            seen_ids.add(res.venue.ref.venue_id)
            out.append((res.venue, res))
        else:
            out.append((_as_map_venue(raw, city), res))
    return out, report


def enrich(swept: list, city: str,
           search: Callable[[str], list[VenueRef]],
           fetch: Callable[[VenueRef], Venue],
           progress: Callable[[int, int, str], None] | None = None,
           ) -> tuple[list[Venue], Report]:
    """Every swept venue, with its page's data wherever the join holds."""
    rows, report = enrich_rows(swept, city, search, fetch, progress)
    return [v for v, _ in rows], report


# --- the real search and fetch ---------------------------------------------

# Here rather than in config: the city search that also used it is gone.
NAME_SEARCH_URL = "https://untappd.com/search"

_VENUE_HREF = re.compile(r"^/v/(?P<slug>[^/]+)/(?P<vid>\d+)")


def search_url_for(query: str) -> str:
    """Untappd's venue search for one name, correctly encoded.

    urlencode, not an f-string: an `&` in a name (`Oak & Ash`) would otherwise
    start a new parameter and search for `Oak `.
    """
    return f"{NAME_SEARCH_URL}?{urlencode({'q': query, 'type': 'venues'})}"


def _clean(text: str | None) -> str | None:
    collapsed = " ".join((text or "").split())
    return collapsed or None


def parse_search_cards(html: str) -> list[VenueRef]:
    """Venue cards on a name search: id, slug, name, and the location line.

    Lenient where the old city-search parser was strict, because a name
    lookup has a different failure: no cards is an ordinary answer (`no_match`,
    visible per venue), not a corpus gate. What matters here is the location
    line -- the last `p.style` line, when it carries no street number -- since
    that is what ranks namesakes. A card whose last line has digits has no
    location line, and it is left empty rather than guessed.
    """
    from bs4 import BeautifulSoup

    refs: list[VenueRef] = []
    for item in BeautifulSoup(html, "lxml").select(".beer-item"):
        anchor = item.select_one('p.name a[href^="/v/"]')
        m = _VENUE_HREF.match(anchor.get("href", "")) if anchor else None
        if not m:
            continue
        lines = [t for p in item.select("p.style") if (t := _clean(p.get_text()))]
        city = lines[-1] if len(lines) >= 2 and not any(
            c.isdigit() for c in lines[-1]) else None
        rest = lines[:-1] if city else lines
        address = next((t for t in rest if any(c.isdigit() for c in t)), None)
        category = next((t for t in rest if t != address), None)
        refs.append(VenueRef(venue_id=m.group("vid"), slug=m.group("slug"),
                             name=_clean(anchor.get_text()) or "(unnamed)",
                             category=category, address=address, city=city))
    return refs


class BrowserNameSearch:
    """Look up one venue name at a time in real Chrome, paced and cached.

    Untappd renders search client-side (Algolia), so plain HTTP gets an empty
    shell; this is the same persistent-profile Chrome the old search used,
    opened once for the whole enrich rather than once per name. Each page
    load is a request to Untappd, so each one is paced with the same jittered
    gap as `PoliteClient` and counted against the same on-disk hourly budget
    -- the old city search counted none of them. Results are cached for
    `cache_ttl_s`, so a re-run after an interruption costs no searches.
    """

    def __init__(self, s: config.Settings, budget: ReadBudget | None = None
                 ) -> None:
        self.s = s
        self._budget = budget if budget is not None else ReadBudget(s)
        self._pw = None
        self._ctx = None
        self._page = None
        self._last = 0.0

    def __enter__(self) -> BrowserNameSearch:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._ctx is not None:
            self._ctx.close()
        if self._pw is not None:
            self._pw.stop()

    def _cache_path(self, query: str) -> Path:
        key = hashlib.sha256(f"name-search:{query}".encode()).hexdigest()
        return config.CACHE_DIR / f"{key}.search.json"

    def _cached(self, query: str) -> list[VenueRef] | None:
        path = self._cache_path(query)
        try:
            if time.time() - path.stat().st_mtime > self.s.cache_ttl_s:
                return None
            return [VenueRef(**r) for r in json.loads(
                path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            return None

    def _store(self, query: str, refs: list[VenueRef]) -> None:
        path = self._cache_path(query)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps([r.__dict__ for r in refs]),
                            encoding="utf-8")
        except OSError as exc:  # a paid search is still used
            log.warning("Could not cache search %r (%s).", query, exc)

    def _pace(self) -> None:
        if self._budget.remaining() <= 0:
            raise BudgetExceeded(
                f"Hourly budget of {self.s.hourly_budget} requests exhausted. "
                "Re-run later; searches and pages already done are cached.")
        wait = random.uniform(self.s.min_delay_s, self.s.max_delay_s)
        elapsed = time.time() - self._last
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._budget.record()

    def _open(self):  # pragma: no cover - needs a real browser
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.s.profile_dir), channel="chrome",
            headless=False,  # headless Chrome is far more likely to be challenged
            user_agent=self.s.user_agent,
            viewport={"width": 1280, "height": 1000})
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self._page

    def __call__(self, query: str) -> list[VenueRef]:
        cached = self._cached(query)
        if cached is not None:
            return cached
        refs = self._load(query)
        self._store(query, refs)
        return refs

    def _load(self, query: str) -> list[VenueRef]:  # pragma: no cover - browser
        from playwright.sync_api import TimeoutError as PWTimeout

        page = self._page or self._open()
        self._pace()
        page.goto(search_url_for(query), wait_until="domcontentloaded")
        self._last = time.time()
        try:
            page.wait_for_selector(".beer-item", timeout=15_000)
        except PWTimeout:
            return []  # no results is an answer; `no_match` records it
        return parse_search_cards(page.content())


def page_fetcher(client: PoliteClient) -> Callable[[VenueRef], Venue]:
    """Fetch and parse one venue page through the paced, cached client."""
    from .parsers import parse_venue_stats

    def fetch(ref: VenueRef) -> Venue:
        try:
            return parse_venue_stats(client.get(ref.url), ref)
        except StatsLoginRequired:
            # Cached, the gated page would outlive the sign-in by 12h and the
            # re-run the remedy asks for would read it again.
            client.forget(ref.url)
            raise

    return fetch
