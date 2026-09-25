"""Strict HTML parsing. Every selector miss raises; nothing returns a silent None.

If Untappd changes their markup we want a stack trace and a saved HTML dump,
not a CSV full of zeros.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from bs4 import BeautifulSoup, Tag

from .config import DEBUG_DIR
from .models import Venue, VenueRef

log = logging.getLogger(__name__)

VENUE_HREF_RE = re.compile(r"^/v/(?P<slug>[^/]+)/(?P<vid>\d+)")
STAT_LABEL_RE = re.compile(r"\b(TOTAL|UNIQUE|MONTHLY|YOU)\b", re.IGNORECASE)
# The (?![A-Za-z]) guard is load-bearing: without it "136 Monthly" parses as
# 136 * 1_000_000, because the M of "Monthly" reads as a millions suffix.
NUMBER_RE = re.compile(r"(\d[\d,\.]*)\s*(?:([kKmM])(?![A-Za-z]))?")

# Ordered list of patterns observed to carry venue coordinates on Untappd venue
# pages. Tried in order; first hit wins. Every hit here is a geocoding request
# we do not pay for.
COORD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"staticmap[^\"']*?center=(-?\d+\.\d+),(-?\d+\.\d+)"),
    re.compile(
        r"data-latitude=[\"'](-?\d+\.\d+)[\"']"
        r"[^>]*?data-longitude=[\"'](-?\d+\.\d+)[\"']"
    ),
    re.compile(r"maps[^\"']*?[?&](?:q|query|daddr|ll)=(-?\d+\.\d+),(-?\d+\.\d+)"),
    re.compile(r"[\"']lat(?:itude)?[\"']\s*:\s*(-?\d+\.\d+)\s*,\s*[\"']l(?:ng|on|ongitude)[\"']\s*:\s*(-?\d+\.\d+)"),
    re.compile(r"\bcenter\s*:\s*\{?\s*lat\s*:\s*(-?\d+\.\d+)\s*,\s*lng\s*:\s*(-?\d+\.\d+)"),
)


class ParseError(RuntimeError):
    """The page did not look the way we require it to look."""


class StatsLoginRequired(ParseError):
    """The venue page hides its stats behind "Log In to view Venue Stats".

    Measured 2026-09-22 against a signed-out session: verified venues still
    show their numbers, every other page shows the login prompt in place of
    them. That is an account problem, not a layout change, and treating it as
    one reported most of a city as `fetch_failed` inside an `ok: true` run.
    """


def dump_debug(name: str, html: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"{name}.html"
    path.write_text(html, encoding="utf-8")
    log.error("Wrote failing HTML to %s", path)


def require_many(node: Tag, selector: str, *, ctx: str, minimum: int = 1) -> list[Tag]:
    found = node.select(selector)
    if len(found) < minimum:
        raise ParseError(
            f"[{ctx}] selector {selector!r} matched {len(found)} nodes, "
            f"expected >= {minimum}. The page layout has probably changed."
        )
    return found


def require_one(node: Tag, selector: str, *, ctx: str) -> Tag:
    return require_many(node, selector, ctx=ctx, minimum=1)[0]


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(text.split())
    return collapsed or None


def parse_count(text: str) -> int | None:
    """12,345 -> 12345 ; 1.2k -> 1200 ; n/a -> None."""
    m = NUMBER_RE.search(text)
    if not m:
        return None
    raw, suffix = m.group(1).replace(",", ""), (m.group(2) or "").lower()
    try:
        value = float(raw)
    except ValueError:
        return None
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(round(value))


def parse_venue_stats(html: str, ref: VenueRef) -> Venue:
    soup = BeautifulSoup(html, "lxml")

    gate = soup.select_one('.stats a[href*="/login"]')
    if gate is not None and "view venue stats" in soup.select_one(
            ".stats").get_text(" ", strip=True).lower():
        raise StatsLoginRequired(
            f"venue {ref.venue_id}: Untappd shows its stats only to a "
            f"signed-in user")

    try:
        stats_block = require_one(soup, ".stats", ctx=f"venue:{ref.venue_id}")
        lis = require_many(
            stats_block, "li", ctx=f"venue-stats:{ref.venue_id}", minimum=2
        )
    except ParseError:
        dump_debug(f"venue_{ref.venue_id}_no_stats", html)
        raise

    values: dict[str, int | None] = {}
    for li in lis:
        text = " ".join(li.get_text(" ", strip=True).split())
        label_match = STAT_LABEL_RE.search(text)
        if not label_match:
            continue  # unknown extra stat -- ignore, do not guess
        # Strip the label before parsing so a label letter can never be
        # mistaken for a magnitude suffix.
        numeric_part = text[: label_match.start()] or text
        values[label_match.group(1).upper()] = parse_count(numeric_part)

    # Fail loudly if none of the four known labels appeared: that means the
    # stats block was found but is no longer the stats block we think it is.
    if not values:
        dump_debug(f"venue_{ref.venue_id}_no_labels", html)
        raise ParseError(
            f"venue {ref.venue_id}: found a .stats block but none of its li "
            "items contained TOTAL/UNIQUE/MONTHLY/YOU."
        )

    lat, lng = extract_coords(html)

    return Venue(
        ref=ref,
        total=values.get("TOTAL"),
        unique=values.get("UNIQUE"),
        monthly=values.get("MONTHLY"),
        you=values.get("YOU"),
        lat=lat,
        lng=lng,
        geo_source="embedded" if lat is not None else "none",
        last_checkin=extract_last_checkin(soup),
        fsq_id=extract_fsq_id(soup),
    )


# The venue's own Foursquare link; a review-place URL elsewhere on the page
# (a comment, a check-in) names some other place.
_FSQ_LINK = 'a[data-href=":foursquare"]'
_FSQ_ID = re.compile(r"/review-place/([0-9a-f]{24})\b")


def extract_fsq_id(soup) -> str:
    """The venue's Foursquare place id, or "" when it has no link."""
    link = soup.select_one(_FSQ_LINK)
    m = _FSQ_ID.search(link.get("href", "")) if link else None
    return m.group(1) if m else ""


def extract_last_checkin(soup) -> str:
    """The newest check-in time on the page's activity feed, as a date.

    The latest of all the feed's times, not the first: order is Untappd's
    to change. A time that does not parse is skipped, never guessed; no
    parseable time at all is "" -- unknown, which is not "never".
    """
    from email.utils import parsedate_to_datetime

    newest = None
    for a in soup.select('a.time[data-href=":feed/viewcheckindate"]'):
        try:
            when = parsedate_to_datetime(a.get_text(strip=True))
        except (TypeError, ValueError, IndexError):
            continue
        if when is not None and (newest is None or when > newest):
            newest = when
    return newest.date().isoformat() if newest else ""


def extract_coords(html: str) -> tuple[float | None, float | None]:
    """Pull lat/lng straight out of the venue page if Untappd embedded them."""
    for pattern in COORD_PATTERNS:
        m = pattern.search(html)
        if not m:
            continue
        try:
            lat, lng = float(m.group(1)), float(m.group(2))
        except (TypeError, ValueError):
            continue
        if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0 and (lat or lng):
            return lat, lng
    return None, None


def assert_corpus_quality(venues: Iterable[Venue], threshold: float) -> None:
    """Abort the whole run rather than emit a plausible-looking wrong CSV."""
    venues = list(venues)
    if not venues:
        raise ParseError("Scrape produced zero venues.")
    good = sum(1 for v in venues if v.has_public_stats)
    ratio = good / len(venues)
    if ratio < threshold:
        raise ParseError(
            f"Only {good}/{len(venues)} venues ({ratio:.0%}) yielded full public "
            f"stats; the required floor is {threshold:.0%}. Refusing to write "
            "output. Inspect debug/*.html -- Untappd markup likely changed."
        )
    log.info("Parse quality: %d/%d venues with full stats (%.0f%%)",
             good, len(venues), ratio * 100)
