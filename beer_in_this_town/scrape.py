"""Untappd venue pages: fetching them, and finding the page for one name.

Collecting a *city* from Untappd's web search is gone. That search matches
venue names, not places -- a Tel Aviv search returned a grill in Encino and a
kitchen in Miami Beach -- so the city now comes from the app's map (`sweep`).

What stays is what enrichment needs:

- `fetch_venues` / `fetch_venue`: read venue pages through the paced client.
- `VenueLookup`: look up ONE swept name and return candidate venue refs. The
  join in `resolve.py` accepts a candidate only when its page's own
  coordinates land near the swept pin, so a name match alone never decides
  anything. Search is client-rendered (Algolia), so this drives the
  browser; anonymous search stops at five results, which is plenty for one
  name.
"""
from __future__ import annotations

import logging
import random
from collections.abc import Callable
from urllib.parse import urlencode

from .config import SEARCH_URL, Settings
from .http_client import (
    BudgetExceeded,
    PoliteClient,
    RateLimitTripped,
    TransportUnavailable,
)
from .models import Venue, VenueRef
from .parsers import parse_search_page, parse_venue_stats

log = logging.getLogger(__name__)

# How many search cards one name lookup keeps. `resolve` fetches at most two
# pages per name anyway; five is what an anonymous search returns.
LOOKUP_RESULTS = 5
RESULTS_TIMEOUT_MS = 20_000


def search_url_for(query: str) -> str:
    """Untappd's venue search for this query, correctly encoded.

    urlencode, not an f-string. Interpolating the query raw meant an `&` in it
    started a new parameter and a `#` turned the rest into a fragment -- so
    "rock & roll" searched for "rock ", returned results, and gave no sign
    anything was wrong.
    """
    return f"{SEARCH_URL}?{urlencode({'q': query, 'type': 'venues'})}"


class VenueLookup:
    """Name -> candidate venue refs, through one reused browser session.

    Use as a context manager; the instance is the `search` callable
    `resolve.enrich` takes. One browser for the whole pass rather than one
    per name, and a jittered pause between lookups at the same pacing as
    every other read -- a fixed, fast cadence is what reads as a script.

    `page` may be injected (anything with goto / wait_for_selector /
    wait_for_timeout / content) so the parsing and pacing are testable
    without a browser.
    """

    def __init__(self, s: Settings, results: int = LOOKUP_RESULTS,
                 page=None, rng: random.Random | None = None) -> None:
        self.s = s
        self.results = results
        self._page = page
        self._rng = rng or random.Random()
        self._pw = None
        self._ctx = None
        self._lookups = 0

    def __enter__(self) -> VenueLookup:
        if self._page is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.s.profile_dir),
                channel="chrome",
                headless=False,  # headless Chrome is far more likely to be challenged
                user_agent=self.s.user_agent,
                viewport={"width": 1280, "height": 1000},
            )
            self._page = (self._ctx.pages[0] if self._ctx.pages
                          else self._ctx.new_page())
        return self

    def __exit__(self, *exc) -> None:
        if self._ctx is not None:
            self._ctx.close()
        if self._pw is not None:
            self._pw.stop()
        self._ctx = self._pw = None

    def _pause(self) -> None:
        gap = self._rng.uniform(self.s.min_delay_s, self.s.max_delay_s)
        self._page.wait_for_timeout(int(gap * 1000))

    def __call__(self, name: str) -> list[VenueRef]:
        if self._page is None:
            raise RuntimeError("VenueLookup must be used as a context manager.")
        if self._lookups:
            self._pause()
        self._lookups += 1
        self._page.goto(search_url_for(name), wait_until="domcontentloaded")
        # Results are rendered client-side, so they do not exist at
        # domcontentloaded. A name with no match never renders any, which is
        # an answer ("no results"), not an error.
        try:
            self._page.wait_for_selector(".beer-item",
                                         timeout=RESULTS_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 -- playwright's TimeoutError
            log.info("No search results rendered for %r.", name)
        refs = parse_search_page(self._page.content(), strict=False)
        return refs[: self.results]


def fetch_venue(client: PoliteClient, ref: VenueRef) -> Venue:
    """One venue page, parsed. Raises on failure; `resolve` records it."""
    return parse_venue_stats(client.get(ref.url), ref)


def fetch_venues(
    client: PoliteClient,
    refs: list[VenueRef],
    progress: Callable[[int, int, VenueRef], None] | None = None,
) -> list[Venue]:
    """Fetch and parse each venue page. Individual failures are logged, counted,
    and left for assert_corpus_quality to judge in aggregate."""
    out: list[Venue] = []
    for i, ref in enumerate(refs, 1):
        if progress:
            progress(i, len(refs), ref)
        try:
            out.append(fetch_venue(client, ref))
        except (RateLimitTripped, BudgetExceeded, TransportUnavailable):
            # These are deliberate stops, not per-venue failures. Swallowing
            # them meant a run that hit a 429 wall kept firing one real request
            # per remaining venue into an active rate-limit -- the exact
            # behaviour PoliteClient exists to prevent.
            log.error("Rate limit reached at venue %d/%d -- aborting the run.",
                      i, len(refs))
            raise
        except Exception as exc:  # one bad venue must not kill the run
            log.error("venue %s (%s) failed: %s", ref.venue_id, ref.name, exc)
            out.append(Venue(ref=ref, total=None, unique=None, monthly=None, you=None))
    return out
