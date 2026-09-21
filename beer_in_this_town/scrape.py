"""Untappd venue pages: fetch them through the paced client and parse them.

Collecting a *city* from Untappd's web search is gone: that search matches
venue names, not places, so the city now comes from the app's map (`sweep`).
Looking up one swept name lives in `resolve.py`, with the join it serves.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from .http_client import (
    BudgetExceeded,
    PoliteClient,
    RateLimitTripped,
    TransportUnavailable,
)
from .models import Venue, VenueRef
from .parsers import parse_venue_stats

log = logging.getLogger(__name__)


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
