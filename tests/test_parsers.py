"""Offline tests for the parsing layer. No network, no browser.

The fixtures below are shaped like real Untappd markup. The numbers in
VENUE_HTML are the real values for American Taproom - Waterloo as of
2026-08-20, so a regression shows up as a concrete wrong number rather than an
abstract failure.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.models import Venue, VenueRef
from beer_in_this_town.parsers import (
    ParseError,
    assert_corpus_quality,
    extract_coords,
    parse_count,
    parse_search_page,
    parse_venue_stats,
)

SEARCH_HTML = """
<div class="beer-item">
  <p class="name"><a href="/v/american-taproom-waterloo/7480946">American Taproom - Waterloo</a>
    <span class="verified small">Verified</span></p>
  <p class="style">American Restaurant, Beer Bar, Dive Bar, Bar</p>
  <p class="style">261 Waterloo St, #01-23</p>
  <p class="style">Singapore, Singapore</p>
</div>
<div class="beer-item">
  <p class="name"><a href="/v/welcome-ren-min/4875014">Welcome Ren Min</a></p>
  <p class="style">Beer Garden, Beer Bar</p>
  <p class="style">1 Kadayanallur Street</p>
  <p class="style">Singapore, Singapore</p>
</div>
"""

# Not every card carries all three style lines. Reading them positionally means
# a missing category silently shifts the address into the category column and
# the city into the address column -- a whole CSV of plausible, wrong data.
SEARCH_HTML_SPARSE = """
<div class="beer-item">
  <p class="name"><a href="/v/no-category/111">No Category</a></p>
  <p class="style">42 Somewhere Road</p>
  <p class="style">Singapore, Singapore</p>
</div>
<div class="beer-item">
  <p class="name"><a href="/v/no-address/222">No Address</a></p>
  <p class="style">Beer Bar</p>
  <p class="style">Singapore, Singapore</p>
</div>
<div class="beer-item">
  <p class="name"><a href="/v/city-only/333">City Only</a></p>
  <p class="style">Singapore, Singapore</p>
</div>
<div class="beer-item">
  <p class="name"><a href="/v/bare/444">Bare</a></p>
</div>
"""

VENUE_HTML = """
<div class="stats">
  <li><span>20,259</span><span>Total</span></li>
  <li><span>2,451</span><span>Unique</span></li>
  <li><span>136</span><span>Monthly</span></li>
  <li><a>0</a><span>You</span></li>
</div>
<img src="https://maps.googleapis.com/maps/api/staticmap?center=1.2982997,103.8520951&zoom=15">
"""


@pytest.fixture
def refs() -> list[VenueRef]:
    return parse_search_page(SEARCH_HTML)


@pytest.fixture
def venue(refs: list[VenueRef]) -> Venue:
    return parse_venue_stats(VENUE_HTML, refs[0])


# --- search results -------------------------------------------------------
@pytest.mark.unit
def test_search_finds_every_venue(refs):
    assert len(refs) == 2


@pytest.mark.unit
def test_search_extracts_identity(refs):
    assert refs[0].venue_id == "7480946"
    assert refs[0].slug == "american-taproom-waterloo"
    assert refs[0].name == "American Taproom - Waterloo"
    assert refs[0].url == "https://untappd.com/v/american-taproom-waterloo/7480946"


@pytest.mark.unit
def test_search_extracts_style_fields(refs):
    assert refs[0].category.startswith("American Restaurant")
    assert refs[0].address == "261 Waterloo St, #01-23"
    assert refs[0].city == "Singapore, Singapore"


@pytest.fixture
def sparse():
    return {r.slug: r for r in parse_search_page(SEARCH_HTML_SPARSE)}


@pytest.mark.unit
def test_missing_category_does_not_shift_the_address_up(sparse):
    # The regression: read positionally, this card reported
    # category="42 Somewhere Road" and address="Singapore, Singapore".
    r = sparse["no-category"]
    assert r.category is None
    assert r.address == "42 Somewhere Road"
    assert r.city == "Singapore, Singapore"


@pytest.mark.unit
def test_missing_address_does_not_shift_the_city_up(sparse):
    r = sparse["no-address"]
    assert r.category == "Beer Bar"
    assert r.address is None
    assert r.city == "Singapore, Singapore"


@pytest.mark.unit
def test_city_only_card_fills_only_the_city(sparse):
    r = sparse["city-only"]
    assert r.category is None
    assert r.address is None
    assert r.city == "Singapore, Singapore"


@pytest.mark.unit
def test_card_with_no_style_lines_is_still_parsed(sparse):
    r = sparse["bare"]
    assert r.name == "Bare"
    assert (r.category, r.address, r.city) == (None, None, None)


@pytest.mark.unit
def test_search_raises_when_markup_changes():
    with pytest.raises(ParseError):
        parse_search_page("<div>nothing recognisable</div>")


@pytest.mark.unit
def test_search_lenient_mode_returns_empty_instead_of_raising():
    # Used when probing pagination, where an empty page is a valid answer.
    assert parse_search_page("<div>nothing</div>", strict=False) == []


# --- venue stats ----------------------------------------------------------
@pytest.mark.unit
def test_stats_match_known_values(venue):
    assert (venue.total, venue.unique, venue.monthly, venue.you) == (
        20259, 2451, 136, 0,
    )


@pytest.mark.unit
def test_coords_are_read_from_the_page(venue):
    assert venue.has_coords
    assert venue.lat == pytest.approx(1.2982997)
    assert venue.lng == pytest.approx(103.8520951)
    assert venue.geo_source == "embedded"


@pytest.mark.unit
def test_stats_raise_when_block_is_missing(refs):
    with pytest.raises(ParseError):
        parse_venue_stats("<div>no stats here</div>", refs[0])


@pytest.mark.unit
def test_stats_raise_when_labels_are_unrecognised(refs):
    html = ('<div class="stats">'
            "<li><span>1</span><span>Wat</span></li>"
            "<li><span>2</span><span>Zzz</span></li></div>")
    with pytest.raises(ParseError):
        parse_venue_stats(html, refs[0])


# --- number parsing -------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("20,259", 20259),
        ("1.2k", 1200),
        ("1.2k Total", 1200),
        ("3M", 3_000_000),
        ("0", 0),
        ("n/a", None),
    ],
)
def test_parse_count(text, expected):
    assert parse_count(text) == expected


@pytest.mark.unit
@pytest.mark.parametrize("text", ["136 Monthly", "5 Multiple", "7 Members"])
def test_label_letter_is_not_a_magnitude_suffix(text):
    """Regression: "136 Monthly" once parsed as 136,000,000.

    The M of "Monthly" was being read as a millions suffix, which silently
    inflated the Monthly column on every venue -- no error, just wrong data.
    Keep the (?![A-Za-z]) guard in NUMBER_RE.
    """
    assert parse_count(text) == int(text.split()[0])


# --- coordinate extraction ------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "html",
    [
        '<img src="...staticmap?center=1.30,103.85&zoom=15">',
        '<div data-latitude="1.30" data-longitude="103.85"></div>',
        '<a href="https://maps.google.com/?daddr=1.30,103.85">go</a>',
        '{"latitude": 1.30, "longitude": 103.85}',
    ],
)
def test_coordinate_patterns(html):
    lat, lng = extract_coords(html)
    assert lat == pytest.approx(1.30)
    assert lng == pytest.approx(103.85)


@pytest.mark.unit
def test_coordinates_absent_is_not_an_error():
    assert extract_coords("<div>no coords</div>") == (None, None)


# --- the corpus quality gate ---------------------------------------------
@pytest.mark.unit
def test_gate_aborts_on_degraded_corpus(refs):
    empty = [Venue(ref=refs[0], total=None, unique=None, monthly=None, you=None)] * 10
    with pytest.raises(ParseError):
        assert_corpus_quality(empty, 0.90)


@pytest.mark.unit
def test_gate_passes_clean_corpus(venue):
    assert_corpus_quality([venue] * 10, 0.90)


@pytest.mark.unit
def test_gate_rejects_empty_corpus():
    with pytest.raises(ParseError):
        assert_corpus_quality([], 0.90)
