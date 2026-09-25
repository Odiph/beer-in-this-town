"""The last check-in date (#21) and the Foursquare id (#9, #6) from a venue page.

Both were already on every page `enrich` fetches: the activity feed dates each
check-in ("Fri, 21 Aug 2026 11:39:39 +0000", newest first), and the venue's
own links include its Foursquare place. The markup below is shaped like the
real page (checked 2026-09-26 against cached pages); no real check-ins.
"""
from __future__ import annotations

from datetime import date

import pytest

from beer_in_this_town import classify
from beer_in_this_town.models import CSV_FIELDS, Venue, VenueRef
from beer_in_this_town.parsers import parse_venue_stats

STATS = """
<div class="stats">
  <li><span>33,480</span><span>Total</span></li>
  <li><span>5,000</span><span>Unique</span></li>
  <li><span>40</span><span>Monthly</span></li>
  <li><a>0</a><span>You</span></li>
</div>"""

FEED = """
<div class="item"><div class="checkin"><div class="feedback"><div class="bottom">
  <a class="time timezoner track-click" data-href=":feed/viewcheckindate"
     href="/user/x/checkin/1">Fri, 19 Jun 2026 12:25:43 +0000</a>
</div></div></div></div>
<div class="item"><div class="checkin"><div class="feedback"><div class="bottom">
  <a class="time timezoner track-click" data-href=":feed/viewcheckindate"
     href="/user/y/checkin/2">Fri, 21 Aug 2026 11:39:39 +0000</a>
</div></div></div></div>"""

LINKS = """
<a class="tw" data-href=":twitter" href="https://twitter.com/somebar">Twitter</a>
<a class="fs track-click" data-track="venue" data-href=":foursquare"
   href="https://foursquare.com/placemakers/review-place/5124ed4ae4b0a2268f3fcfb3?ref=X">Foursquare</a>"""

REF = VenueRef(venue_id="1", slug="some-bar", name="Some Bar", category="Bar",
               address="1 High St", city="Tel Aviv")


@pytest.mark.unit
def test_the_latest_feed_time_is_the_last_checkin_whatever_the_order():
    v = parse_venue_stats(STATS + FEED + LINKS, REF)
    assert v.last_checkin == "2026-08-21"


@pytest.mark.unit
def test_the_foursquare_id_comes_from_the_venues_own_link():
    v = parse_venue_stats(STATS + FEED + LINKS, REF)
    assert v.fsq_id == "5124ed4ae4b0a2268f3fcfb3"


@pytest.mark.unit
def test_a_foursquare_link_elsewhere_on_the_page_is_not_the_venues():
    # Only the venue's own social link counts; a review-place URL in, say, a
    # check-in comment would name some other place.
    stray = ('<p>see https://foursquare.com/placemakers/review-place/'
             'aaaaaaaaaaaaaaaaaaaaaaaa</p>')
    v = parse_venue_stats(STATS + FEED + stray, REF)
    assert v.fsq_id == ""


@pytest.mark.unit
def test_no_feed_and_no_link_are_unknown_not_invented():
    v = parse_venue_stats(STATS, REF)
    assert (v.last_checkin, v.fsq_id) == ("", "")


@pytest.mark.unit
def test_an_unparseable_time_is_skipped_not_guessed():
    odd = FEED.replace("Fri, 21 Aug 2026 11:39:39 +0000", "yesterday")
    assert parse_venue_stats(STATS + odd, REF).last_checkin == "2026-06-19"


@pytest.mark.unit
def test_both_fields_survive_the_csv_round_trip():
    v = parse_venue_stats(STATS + FEED + LINKS, REF)
    row = v.to_row()
    assert CSV_FIELDS[-2:] == ["last_checkin", "fsq_id"], \
        "appended, so older column orders still line up"
    back = Venue.from_row({k: str(val) for k, val in row.items()})
    assert (back.last_checkin, back.fsq_id) == ("2026-08-21",
                                               "5124ed4ae4b0a2268f3fcfb3")


@pytest.mark.unit
def test_an_older_csv_without_the_columns_reads_as_unknown():
    old = {"venue_id": "1", "name": "Some Bar", "total": "10"}
    v = Venue.from_row(old)
    assert (v.last_checkin, v.fsq_id) == ("", "")


def _venue(**kw) -> Venue:
    base = dict(ref=REF, total=500, unique=300, monthly=0, you=0)
    base.update(kw)
    return Venue(**base)


@pytest.mark.unit
@pytest.mark.parametrize("last, flagged", [
    ("2026-09-20", False),   # 6 days ago: alive, monthly == 0 was rounding
    ("2026-04-01", False),   # ~6 months: seasonal or fading, not closed
    ("2025-06-01", True),    # over a year: very likely gone
])
def test_a_known_last_checkin_decides_the_closure_flag(last, flagged):
    v = _venue(monthly=0, last_checkin=last)
    assert classify.looks_closed(v, today=date(2026, 9, 26)) is flagged


@pytest.mark.unit
def test_a_busy_monthly_count_cannot_hide_a_stale_date_or_vice_versa():
    # The date wins when it is known: it is the direct observation.
    assert classify.looks_closed(_venue(monthly=5, last_checkin="2024-01-01"),
                                 today=date(2026, 9, 26)) is True


@pytest.mark.unit
def test_without_a_date_the_monthly_rule_still_applies():
    assert classify.looks_closed(_venue(monthly=0, total=500),
                                 today=date(2026, 9, 26)) is True
    assert classify.looks_closed(_venue(monthly=3, total=500),
                                 today=date(2026, 9, 26)) is False
