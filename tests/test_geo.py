"""A Tel Aviv search returned a restaurant in Encino, California.

21 of 100 venues on the first live run were over 100 km from Tel Aviv, because
Untappd's search matches venue *names*, not geography — and since `--count`
caps the total, those 21 crowded out 21 real Tel Aviv bars. Every venue
carried coordinates the whole time; nothing compared them to anything.

The rule under test is the one this project keeps arriving at: a venue whose
distance cannot be established is **kept**, not dropped. No coordinates means
unknown, and cutting on unknown is how a correct-looking corpus quietly loses
real venues.

No network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.geo import (
    haversine_km,
    parse_radius,
    within_radius,
)
from beer_in_this_town.models import Venue, VenueRef

TLV = (32.0853, 34.7818)


def _venue(name: str, lat: float | None = None, lng: float | None = None) -> Venue:
    return Venue(
        ref=VenueRef(venue_id=name, slug=name.lower(), name=name,
                     category="Bar", address=None, city=None),
        total=100, unique=10, monthly=5, you=None, lat=lat, lng=lng,
    )


# --- distance -------------------------------------------------------------

@pytest.mark.unit
def test_a_venue_in_the_city_is_close():
    assert haversine_km(TLV, (32.0700, 34.7700)) < 3


@pytest.mark.unit
def test_the_encino_case():
    """The actual row that motivated this: `Tel Aviv Grill`, Encino CA."""
    km = haversine_km(TLV, (34.1594, -118.5012))
    assert 11_500 < km < 12_500, f"got {km:.0f}km"


@pytest.mark.unit
def test_distance_is_symmetric():
    a, b = TLV, (51.5074, -0.1278)
    assert haversine_km(a, b) == pytest.approx(haversine_km(b, a))


@pytest.mark.unit
def test_zero_distance_to_itself():
    assert haversine_km(TLV, TLV) == pytest.approx(0.0, abs=1e-9)


# --- the filter -----------------------------------------------------------

@pytest.mark.unit
def test_far_venues_are_dropped_and_named():
    near = _venue("Lauter", 32.0700, 34.7700)
    far = _venue("Tel Aviv Grill", 34.1594, -118.5012)

    out = within_radius([near, far], TLV, 15)

    assert [v.ref.name for v in out.kept] == ["Lauter"]
    assert out.worst_offenders()[0][0] == "Tel Aviv Grill"
    assert out.summary["dropped"] == 1


@pytest.mark.unit
def test_a_venue_without_coordinates_is_kept():
    """Unknown is not far. Dropping on unknown loses real venues silently."""
    placed = _venue("Lauter", 32.0700, 34.7700)
    unplaced = _venue("Somewhere", None, None)

    out = within_radius([placed, unplaced], TLV, 15)

    assert unplaced in out.kept, "a venue was cut for having no coordinates"
    assert out.summary["unplaceable"] == 1
    assert out.summary["dropped"] == 0


@pytest.mark.unit
def test_the_boundary_is_inclusive():
    """A venue exactly at the radius is inside it, not a rounding casualty."""
    from beer_in_this_town.geo import distance_from

    v = _venue("Edge", 32.2000, 34.7818)
    km = distance_from(TLV, v)
    assert within_radius([v], TLV, km).summary["kept"] == 1


@pytest.mark.unit
def test_a_radius_of_zero_is_refused():
    with pytest.raises(ValueError):
        within_radius([], TLV, 0)


@pytest.mark.unit
def test_the_result_can_account_for_every_venue():
    """A filter that silently halves a corpus looks like a broken scrape."""
    vs = [_venue("A", 32.07, 34.77), _venue("B", 34.15, -118.50),
          _venue("C", None, None)]
    out = within_radius(vs, TLV, 15)
    assert len(out.kept) + len(out.dropped) == len(vs)


# --- units ----------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("raw,km", [
    ("15", 15.0), ("15km", 15.0), ("15 km", 15.0), ("15KM", 15.0),
    ("9mi", 14.484096), ("500m", 0.5),
])
def test_radius_units(raw, km):
    assert parse_radius(raw) == pytest.approx(km)


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "   ", "soon", "15 parsecs", "-5km", "0"])
def test_an_unreadable_radius_is_refused(raw):
    """Guessing a unit is how somebody asks for 9 miles and gets 9 km."""
    with pytest.raises(ValueError):
        parse_radius(raw)
