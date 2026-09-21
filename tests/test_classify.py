"""Candidate venue heuristics. Every threshold here is a guess until #10 runs.

These exist to be measured, not to be trusted. They are deliberately not wired
into `run`: shipping an unvalidated classifier into the export path would
reintroduce exactly the failure this repo defends against everywhere else --
plausible output, wrong at an unknown rate, and nothing raises.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.classify import (
    Kind,
    Verdict,
    classify,
    venue_kind,
)
from beer_in_this_town.models import Venue, VenueRef


def _venue(category=None, total=500, unique=300, monthly=20, name="Ghost Whale"):
    return Venue(
        ref=VenueRef(venue_id="1", slug="v", name=name, category=category,
                     address="1 Some Road", city="London"),
        total=total, unique=unique, monthly=monthly, you=None,
    )


# --- kind, from category text only ----------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("category,expected", [
    ("Brewery", Kind.BREWERY),
    ("Taproom", Kind.BREWERY),
    ("Brewpub", Kind.BREWERY),
    ("Bottle Shop", Kind.BOTTLE_SHOP),
    ("Liquor Store", Kind.BOTTLE_SHOP),
    ("Beer Bar", Kind.CRAFT_BEER_BAR),
    ("Craft Beer Bar", Kind.CRAFT_BEER_BAR),
    ("Café", Kind.NON_BEER),
    ("Coffee Shop", Kind.NON_BEER),
])
def test_unambiguous_categories_are_settled(category, expected):
    assert venue_kind(category) is expected


@pytest.mark.unit
@pytest.mark.parametrize("category", ["Bar", "Pub", None, "", "Cocktail Bar"])
def test_ambiguous_categories_stay_unsettled(category):
    """#6 is explicit: a bare 'Bar' is recorded for plenty of serious craft
    venues, so a rule mapping bar -> general would discard what the tool is
    for. Unsettled is a third outcome, not a rejection."""
    assert venue_kind(category) is Kind.UNSETTLED


@pytest.mark.unit
def test_a_beer_signal_beats_a_restaurant_signal_in_the_same_line():
    """Real Untappd data: 'American Restaurant, Beer Bar, Dive Bar, Bar'.

    Reading only the first category would file this taproom as a restaurant.
    """
    assert venue_kind("American Restaurant, Beer Bar, Dive Bar, Bar") is (
        Kind.CRAFT_BEER_BAR)


@pytest.mark.unit
def test_a_brewery_inside_a_restaurant_line_is_still_a_brewery():
    assert venue_kind("Restaurant, Brewery") is Kind.BREWERY


# --- private spaces (#8) ---------------------------------------------------
@pytest.mark.unit
def test_a_handful_of_unique_visitors_reads_as_private():
    """A home has 1-2 unique visitors and can still have hundreds of check-ins."""
    v = _venue(category="Bar", total=400, unique=2, monthly=8)
    assert classify(v).verdict is Verdict.DROP
    assert classify(v).reason.startswith("private")


@pytest.mark.unit
def test_a_busy_venue_is_never_private():
    assert classify(_venue(category="Beer Bar", unique=800)).verdict is Verdict.KEEP


@pytest.mark.unit
def test_a_brand_new_venue_is_not_mistaken_for_a_home():
    """Low unique AND low total is a new or quiet venue, not a household.

    The household pattern is many check-ins concentrated in few people; both
    numbers being small is just a venue nobody has been to yet.
    """
    v = _venue(category="Beer Bar", total=3, unique=2, monthly=3)
    assert classify(v).verdict is not Verdict.DROP or "private" not in classify(v).reason


# --- closure candidates (#7) ----------------------------------------------
@pytest.mark.unit
def test_a_healthy_total_with_no_monthly_activity_is_a_closure_candidate():
    v = _venue(category="Beer Bar", total=4000, unique=900, monthly=0)
    c = classify(v)
    assert c.verdict is Verdict.DROP and c.reason.startswith("closed")


@pytest.mark.unit
def test_a_quiet_but_new_venue_is_not_flagged_as_closed():
    """#7 admits the heuristic is suggestive; keep it off small totals."""
    v = _venue(category="Beer Bar", total=12, unique=9, monthly=0)
    assert classify(v).reason.startswith("closed") is False


# --- missing data ----------------------------------------------------------
@pytest.mark.unit
def test_missing_stats_are_never_read_as_a_verdict():
    """A venue whose stats failed to parse must not be judged on them."""
    v = _venue(category="Beer Bar", total=None, unique=None, monthly=None)
    c = classify(v)
    assert c.verdict is not Verdict.DROP


# --- the costly error is ordered first ------------------------------------
@pytest.mark.unit
def test_private_outranks_every_other_signal():
    """A wrong keep is someone's front door; it decides before anything else."""
    v = _venue(category="Brewery", total=400, unique=1, monthly=0)
    assert classify(v).reason.startswith("private")


@pytest.mark.unit
def test_every_venue_lands_in_exactly_one_named_bucket():
    """`score` stratifies on these, so the bucket set has to be closed."""
    for cat in ("Brewery", "Bar", "Café", None, "Bottle Shop"):
        assert classify(_venue(category=cat)).bucket
