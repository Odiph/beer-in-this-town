"""A whole city at one page every four seconds is the shape of a script.

The user asked that enrich not be obviously a machine. The rhythm only ever
adds pauses; the per-request floors and the hourly ceiling are unchanged.
No network.
"""
from __future__ import annotations

import random

import pytest

from beer_in_this_town.resolve import ReadingRhythm, enrich_rows

pytestmark = pytest.mark.unit


def test_pauses_between_venues_and_breaks_now_and_then():
    slept: list[float] = []
    rhythm = ReadingRhythm(sleep=slept.append, rng=random.Random(7))
    for i in range(1, 101):
        rhythm(i)
    lo, hi = ReadingRhythm.VENUE_GAP_S
    breaks = [s for s in slept if s >= ReadingRhythm.BREAK_S[0]]
    gaps = [s for s in slept if s < ReadingRhythm.BREAK_S[0]]
    assert all(lo <= s <= hi for s in gaps)
    assert 2 <= len(breaks) <= 5, breaks        # every 20-40 of 100 venues
    assert len(set(round(s, 3) for s in gaps)) > 50, "the gap must vary"


def test_enrich_rests_between_venues_not_after_the_last():
    rests: list[int] = []
    from beer_in_this_town.app_sweep import Venue

    swept = [Venue(n, 0, 0, 32.07, 34.78) for n in "ABC"]
    enrich_rows(swept, "Tel Aviv", search=lambda q: [],
                fetch=lambda ref: None, rest=rests.append)
    assert rests == [1, 2]


def test_a_venue_served_from_cache_is_not_paced():
    """Resuming after an interruption must not re-wait for cached venues."""
    slept: list[float] = []
    count = {"n": 0}
    rhythm = ReadingRhythm(sleep=slept.append, rng=random.Random(1),
                           requests_used=lambda: count["n"])
    rhythm(1)                      # cached: nothing fetched
    count["n"] += 2
    rhythm(2)                      # fetched a search and a page
    rhythm(3)                      # cached again
    assert len(slept) == 1
