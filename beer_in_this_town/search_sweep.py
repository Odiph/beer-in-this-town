"""`sweep --method search`: a city's venues from Untappd's web search.

The search is text, not geography: it matches a venue's name and city
line, and ranks by all-time check-ins (measured: 0 of 325 pairs out of
order). So each spelling of the city's name (`city_names`) is searched
once, most-checked-in first, down to `--top` results -- at most 1,000,
which is as deep as the site pages. Those are exactly the variant's
most-checked-in venues; what the cap leaves out is the long tail.

Every result is a venue page with an id, so `enrich` fetches it directly
instead of guessing which page a map pin belongs to. What the search does
not give is a position: that comes from the page, in `enrich`, which also
drops a namesake whose page puts it outside the city.

Measured 2026-09-23 against the app-map sweep: London's popularity top
1,000 held 99 of the map sweep's top 100 venues by check-ins, for 50
requests; Tel Aviv's held every one of its top 100 beer venues, where the
map sweep had 20.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

from .agent_io import Envelope, Problem, fail
from .city_names import CityNames, for_city, norm
from .config import CACHE_DIR, SWEEP_CSV, Settings, cli_arg, stage_path
from .export import write_csv, write_geojson, write_gpx, write_kml
from .models import Venue, VenueRef
from .state import record_run

log = logging.getLogger(__name__)

SEARCH_CAP = 1000   # the site pages no deeper than this, whatever it reports
PAGE_SIZE = 20
DEFAULT_TOP = SEARCH_CAP
PY = "python -m beer_in_this_town"


@dataclass(frozen=True)
class SearchPage:
    total: int | None          # what the page reports, which may exceed the cap
    refs: list[VenueRef]
    # False when "Show More" stopped loading before `top` or the end: the
    # page is short because it failed, not because the rest is unpopular.
    complete: bool = True


Load = Callable[[str, int], SearchPage]


class SearchPageUnreadable(RuntimeError):
    """A search page showed neither results nor its empty state."""


@dataclass
class Harvest:
    venues: list[Venue]
    per_variant: dict[str, dict]
    warnings: list[str] = field(default_factory=list)


def _words(text: str | None) -> set[str]:
    return set(norm((text or "").replace(",", " ")).split())


def harvest(names: CityNames, load: Load, top: int) -> Harvest:
    """Every variant's top results in the city, merged, in variant order.

    Ranks from two searches are not comparable, so the first (commonest)
    spelling's order leads and a later variant adds only what it alone found.
    """
    if top < 1:
        raise ValueError("--top must be at least 1")
    top = min(top, SEARCH_CAP)
    variant_words = [set(v.split()) for v in names.variants]
    found: dict[str, VenueRef] = {}   # insertion order is the answer
    per_variant: dict[str, dict] = {}
    warnings: list[str] = []
    for variant in names.variants:
        page = load(variant, top)
        other = 0
        for ref in page.refs:
            line = _words(ref.city)
            if not any(w <= line for w in variant_words):
                other += 1   # matched on its name; its city line is elsewhere
                continue
            found.setdefault(ref.venue_id, ref)
        per_variant[variant] = {"total": page.total,
                                "collected": len(page.refs),
                                "other_city": other,
                                "complete": page.complete}
        if not page.complete:
            warnings.append(
                f"{variant!r} stopped loading at {len(page.refs)} of "
                f"{min(page.total or 0, top) or 'its'} results: a page of "
                f"results did not load. The ones it did load are kept; re-run "
                f"the sweep to retry the rest.")
        elif (page.total or 0) > len(page.refs):
            warnings.append(
                f"{variant!r} reports {page.total} venues; the "
                f"{len(page.refs)} most-checked-in were collected. The rest "
                f"are less-visited venues.")
    venues = [Venue(ref, None, None, None, None) for ref in found.values()]
    return Harvest(venues, per_variant, warnings)


def cmd_search_sweep(s: Settings | None, city: str, *, top: int = DEFAULT_TOP,
                     formats: tuple[str, ...] = (), title: str = "",
                     load: Load | None = None,
                     names: CityNames | None = None) -> Envelope:
    """Search the city's name variants and write `1_sweep.csv`.

    `load` and `names` are injected by tests; left out, the real ones are
    used: the cached name variants, and the signed-in Chrome profile paced
    and counted like `enrich`.
    """
    names = names or for_city(city, s)
    if load is None:
        return _live(s, city, top, formats, title, names)
    return _run(city, top, formats, title, names, load)


def _run(city: str, top: int, formats: tuple[str, ...], title: str,
         names: CityNames, load: Load) -> Envelope:
    try:
        h = harvest(names, load, top)
    except SearchPageUnreadable as exc:
        return fail("sweep", Problem(
            code="search_unavailable",
            message=str(exc),
            remedy="Usually a signed-out Untappd session, or a page that did "
                   "not load. Run `python -m beer_in_this_town verify "
                   "--json`, then re-run the sweep; searches already done "
                   "are cached. Nothing was written.",
        ), method="search")
    if not h.venues:
        return fail("sweep", Problem(
            code="city_not_found",
            message=f"Untappd's search has no venues listed in {city!r} "
                    f"(searched: {', '.join(names.variants)}).",
            remedy="Ask the human for a fuller city name, as venues list it "
                   "(with country or state), or use --method map.",
        ), method="search", variants=h.per_variant)
    target = stage_path(city, SWEEP_CSV)
    target.parent.mkdir(parents=True, exist_ok=True)
    csv_path = write_csv(h.venues, target)
    base = target.with_suffix("")
    writers = {"kml": lambda p: write_kml(h.venues, p, title),
               "geojson": lambda p: write_geojson(h.venues, p),
               "gpx": lambda p: write_gpx(h.venues, p, title)}
    maps = {f: str(writers[f](base.with_suffix(f".{f}")))
            for f in formats if f in writers}
    record_run(query=city, map_title=title, csv_path=csv_path,
               method="search")
    warnings = ([names.warning] if names.warning else []) + h.warnings + [
        "Positions and check-in counts come from each venue's page: run "
        "enrich. It also drops a namesake whose page is outside the city."]
    return Envelope(
        command="sweep", ok=True,
        data={"city": city, "method": "search", "venues": len(h.venues),
              "located": 0, "csv": str(csv_path), "maps": maps,
              "variants": h.per_variant, "top": min(top, SEARCH_CAP),
              "variants_source": names.source or "the city as typed"},
        warnings=warnings,
        next_actions=[f"{PY} enrich --city {cli_arg(city)} --json"],
    )


def _live(s: Settings, city: str, top: int, formats: tuple[str, ...],
          title: str, names: CityNames) -> Envelope:  # pragma: no cover
    from .http_client import PoliteClient, _cookies_from_storage_state

    if not _cookies_from_storage_state(s.storage_state, "untappd.com"):
        return fail("sweep", Problem(
            code="not_signed_in",
            message="No Untappd session: the venue search returns nothing "
                    "signed out.",
            remedy="Ask the human to sign in to untappd.com: run `beertown "
                   "ui` and use the Accounts step. Then `verify --json`."))
    with PoliteClient(s) as client:
        if s.respect_robots and client.robots_disallows_scraping():
            return fail("sweep", Problem(
                code="robots_disallow",
                message="Untappd's robots.txt disallows the search pages.",
                remedy="Stop and ask a human. Nothing was fetched."))
        with BrowserCitySearch(s, client._budget) as search:
            return _run(city, top, formats, title, names, search)


# --- the real search: Chrome, paced and counted ----------------------------

_TOTAL = re.compile(r"([\d,]+)\s+venue results")


class BrowserCitySearch:  # pragma: no cover - needs a real browser
    """One variant's popularity-sorted results, paged in Chrome, cached.

    Shares `BrowserNameSearch`'s profile, pacing and on-disk hourly budget:
    the page load and every "Show More" are one request each.
    """

    def __init__(self, s: Settings, budget) -> None:
        from .resolve import BrowserNameSearch

        self.s = s
        self._browser = BrowserNameSearch(s, budget=budget)

    def __enter__(self) -> BrowserCitySearch:
        return self

    def __exit__(self, *exc: object) -> None:
        self._browser.__exit__(*exc)

    def _cache(self, query: str, top: int):
        key = hashlib.sha256(f"city-search:{query}:{top}".encode()).hexdigest()
        return CACHE_DIR / f"{key}.citysearch.json"

    def __call__(self, query: str, top: int) -> SearchPage:
        path = self._cache(query, top)
        try:
            if time.time() - path.stat().st_mtime <= self.s.cache_ttl_s:
                rec = json.loads(path.read_text(encoding="utf-8"))
                return SearchPage(rec["total"],
                                  [VenueRef(**r) for r in rec["refs"]])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        page = self._load(query, top)
        if not page.complete:
            return page   # cached, a re-run would retry nothing
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"total": page.total, "refs": [
            r.__dict__ for r in page.refs]}, ensure_ascii=False),
            encoding="utf-8")
        return page

    def _request(self, action) -> None:
        b = self._browser
        b._pace()
        action()
        b._last = time.time()

    def _load(self, query: str, top: int) -> SearchPage:
        from .resolve import NAME_SEARCH_URL, parse_search_cards

        b = self._browser
        page = b._page or b._open()
        url = f"{NAME_SEARCH_URL}?" + urlencode(
            {"q": query, "type": "venues", "sort": "all"})
        self._request(lambda: page.goto(url, wait_until="domcontentloaded"))
        try:
            page.wait_for_selector(".beer-item", timeout=30_000)
        except Exception:
            pass  # no results, or not rendered: the header or empty-state says
        page.wait_for_timeout(1500)
        head = page.locator("text=/venue results for/i").first
        head = head.inner_text() if head.count() else ""
        if not head:
            if page.locator("#algolia-hits .results-none").count():
                return SearchPage(0, [])
            raise SearchPageUnreadable(
                f"Untappd's search page for {query!r} showed "
                               f"neither results nor its empty state.")
        m = _TOTAL.search(head)
        total = int(m.group(1).replace(",", "")) if m else None
        seen = len(parse_search_cards(page.content()))
        complete = True
        while seen < top:
            more = page.locator("#algolia-show-more a.more_search").first
            if not more.count() or not more.is_visible():
                break   # the end of the results, or the site's cap
            now = seen
            for _attempt in range(2):   # one retry: a slow page is common
                self._request(more.click)
                deadline = time.time() + 15
                while time.time() < deadline and now <= seen:
                    page.wait_for_timeout(800)
                    now = len(parse_search_cards(page.content()))
                if now > seen:
                    break
            if now <= seen:
                complete = False   # stalled: said so, never cached
                break
            seen = now
        return SearchPage(total, parse_search_cards(page.content())[:top],
                          complete=complete)
