"""Search pagination + venue detail fetching.

The Show More mechanism is the one part of Untappd that cannot be verified
without hitting the live site, so this module probes rather than assumes: it
tries a set of offset-style query parameters, checks whether each yields
genuinely new venue IDs, and falls back to driving a real browser if none work.
"""
from __future__ import annotations

import logging
import re
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
from .parsers import (
    ClientRenderedSearch,
    ParseError,
    parse_search_page,
    parse_venue_stats,
)

log = logging.getLogger(__name__)

# Candidate pagination params, most likely first.
PAGINATION_PARAMS = ("offset", "start", "page")
PAGE_SIZE_GUESS = 25


# Anonymous Algolia search returns five results and then swaps Show More for a
# sign-in wall. Match the container id and the copy: either alone is enough.
LOGIN_GATE_RE = re.compile(
    r"algolia-login-gate|please sign in to view more", re.IGNORECASE
)


class PaginationUnsupported(RuntimeError):
    """None of the HTTP pagination schemes produced new results."""


class SearchLoginRequired(RuntimeError):
    """Search stopped early at Untappd's sign-in wall, not at the last result."""


def assert_not_login_gated(
    refs: list[VenueRef], html: str, target_count: int
) -> None:
    """Refuse a result set that a sign-in wall cut short.

    Five venues out of a requested hundred is a stopped run, and returning it
    quietly writes a five-row CSV, reports ok, and lets every downstream number
    be wrong by a factor of twenty. A short set is only trustworthy when
    nothing was blocking the way.
    """
    if len(refs) >= target_count or not LOGIN_GATE_RE.search(html):
        return
    raise SearchLoginRequired(
        f"Search stopped at {len(refs)} of {target_count} requested venues "
        f"because Untappd replaced Show More with a sign-in wall. Anonymous "
        f"search is capped at five results."
    )


def _dedupe_extend(acc: dict[str, VenueRef], refs: list[VenueRef]) -> int:
    before = len(acc)
    for r in refs:
        acc.setdefault(r.venue_id, r)
    return len(acc) - before


def search_via_http(client: PoliteClient, s: Settings) -> list[VenueRef]:
    """Paginate the search endpoint over plain HTTP."""
    base_params = {"q": s.query, "type": "venues"}
    first_html = client.get(SEARCH_URL, params=base_params)
    acc: dict[str, VenueRef] = {}
    _dedupe_extend(acc, parse_search_page(first_html))
    log.info("Search page 1: %d venues", len(acc))

    if len(acc) >= s.target_count:
        return list(acc.values())[: s.target_count]

    page_size = len(acc) or PAGE_SIZE_GUESS

    for param in PAGINATION_PARAMS:
        probe_value = page_size if param != "page" else 2
        probe = client.get(
            SEARCH_URL, params={**base_params, param: probe_value}, xhr=True
        )
        gained = _dedupe_extend(acc, parse_search_page(probe, strict=False))
        if gained == 0:
            log.debug("Pagination param %r produced no new venues; trying next", param)
            continue

        log.info("Pagination via %r works (+%d venues)", param, gained)
        step = 2 if param == "page" else 2 * page_size
        stalls = 0
        while len(acc) < s.target_count and stalls < 2:
            html = client.get(SEARCH_URL, params={**base_params, param: step}, xhr=True)
            gained = _dedupe_extend(acc, parse_search_page(html, strict=False))
            log.info("offset %s=%s -> %d total venues", param, step, len(acc))
            stalls = stalls + 1 if gained == 0 else 0
            step += 1 if param == "page" else page_size
        return list(acc.values())[: s.target_count]

    raise PaginationUnsupported(
        "No offset-style query parameter yielded new search results. "
        "Falling back to browser-driven Show More."
    )


def search_url_for(query: str) -> str:
    """Untappd's venue search for this query, correctly encoded.

    urlencode, not an f-string. Interpolating the query raw meant an `&` in it
    started a new parameter and a `#` turned the rest into a fragment -- so
    "rock & roll" searched for "rock ", returned results, and gave no sign
    anything was wrong. The HTTP path has always passed `params=`; this is the
    path that actually runs now that search is client-rendered.
    """
    return f"{SEARCH_URL}?{urlencode({'q': query, 'type': 'venues'})}"


def search_via_browser(s: Settings) -> list[VenueRef]:
    """Fallback: drive real Chrome and click Show More until we have enough."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    url = search_url_for(s.query)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(s.profile_dir),
            channel="chrome",
            headless=False,  # headless Chrome is far more likely to be challenged
            user_agent=s.user_agent,
            viewport={"width": 1280, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")

        # Results are rendered client-side (Algolia injects them into
        # #algolia-hits), so they do not exist at domcontentloaded. Counting
        # straight away sees zero items and concludes the page is empty.
        try:
            page.wait_for_selector(".beer-item", timeout=30_000)
        except PWTimeout:
            log.warning(
                "No .beer-item appeared within 30s -- the query may genuinely "
                "have no results, or the search markup changed again."
            )

        # Show More has carried several class names over the years; match on the
        # accessible name, which is stable.
        more = page.get_by_role("link", name="Show More").or_(
            page.get_by_role("button", name="Show More")
        )
        for _ in range(40):  # hard ceiling: 40 clicks ~= 1000 venues
            count = page.locator(".beer-item").count()
            if count >= s.target_count:
                break
            if more.count() == 0 or not more.first.is_visible():
                log.info("No Show More control left; stopping at %d venues", count)
                break
            more.first.click()
            # Wait for the item count to actually grow -- the auto-waiting that
            # synthetic CDP clicks lack.
            page.wait_for_function(
                "n => document.querySelectorAll('.beer-item').length > n",
                arg=count, timeout=15_000,
            )
            page.wait_for_timeout(int(s.min_delay_s * 1000))  # stay polite

        html = page.content()
        ctx.close()

    refs = parse_search_page(html)
    log.info("Browser search collected %d venues", len(refs))
    assert_not_login_gated(refs, html, s.target_count)
    return refs[: s.target_count]


def collect_venue_refs(
    client: PoliteClient, s: Settings, force_browser: bool = False
) -> list[VenueRef]:
    if force_browser:
        return search_via_browser(s)
    try:
        return search_via_http(client, s)
    except PaginationUnsupported as exc:
        log.warning("%s", exc)
        return search_via_browser(s)
    except ClientRenderedSearch as exc:
        # The routine case since Untappd moved search to Algolia: the HTTP
        # response is the container and nothing else. Expected, so it is not a
        # warning -- it is what the browser path exists for.
        log.info("%s Falling back to the browser path.", exc)
        return search_via_browser(s)
    except ParseError as exc:
        # Something else about the search page stopped parsing. The browser
        # re-parses with the same strict selectors, so this is an attempt, not
        # a workaround; if the markup really changed it fails there too.
        log.warning(
            "Search page did not parse (%s). Trying the browser path, which "
            "applies the same strict selectors.", exc,
        )
        return search_via_browser(s)


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
            html = client.get(ref.url)
            out.append(parse_venue_stats(html, ref))
        except (RateLimitTripped, BudgetExceeded, TransportUnavailable):
            # These are deliberate stops, not per-venue failures. Swallowing
            # them meant a run that hit a 429 wall kept firing one real request
            # per remaining venue into an active rate-limit -- the exact
            # behaviour PoliteClient exists to prevent.
            #
            # TransportUnavailable belongs here for the same reason and was
            # missed: it is a plain RuntimeError, so the broad handler below
            # caught it and the trip added to the client never reached the
            # caller. The run still slept its way through every venue.
            log.error("Rate limit reached at venue %d/%d -- aborting the run.",
                      i, len(refs))
            raise
        except Exception as exc:  # one bad venue must not kill the run
            log.error("venue %s (%s) failed: %s", ref.venue_id, ref.name, exc)
            out.append(Venue(ref=ref, total=None, unique=None, monthly=None, you=None))
    return out
