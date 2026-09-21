"""Turning screen positions into ground, so a cell is a place and not a guess.

The sweep subdivides in *pixels* -- quarter-viewport pans -- which works and
tells you nothing about where you have been. Without ground coordinates:

- coverage cannot be reported ("we swept Tel Aviv" is unverifiable)
- a resumed sweep cannot tell which ground it already covered
- venues arrive with names and screen positions but no location

All three need the same thing: the scale of the map, in metres per pixel.

**Panning cannot supply it.** A swipe tells you how many pixels the map
moved, with no external reference to convert pixels to degrees. Calibration
therefore needs one known point, and the app gives one for free: a city
search centres the map on that city, which `geocode.py` can resolve.

No device, no network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.app_geo import (
    CITY_ZOOM_M_PER_PX,
    Scale,
    cell_for_viewport,
    scale_from_known_points,
    to_latlng,
)
from beer_in_this_town.app_map import Pin
from beer_in_this_town.app_sweep import Cell

TLV = (32.0853, 34.7818)


# --- the scale ------------------------------------------------------------

def test_the_default_scale_is_the_measured_one():
    """10.2 m/px at the zoom a city search lands on, measured on the live
    app from pins whose coordinates were known."""
    assert pytest.approx(10.2, abs=0.5) == CITY_ZOOM_M_PER_PX


def test_scale_can_be_refitted_from_two_known_points():
    """The constant is zoom-dependent. Two pins with known coordinates are
    enough to replace it, which is how a different zoom gets calibrated."""
    a = (Pin("a", 100, 500), 32.0853, 34.7818)
    b = (Pin("b", 600, 500), 32.0853, 34.8318)   # 500 px east, 0.05 deg
    scale = scale_from_known_points([a, b])
    # 0.05 deg lng at this latitude is about 4.7 km over 500 px.
    assert scale.m_per_px == pytest.approx(9.4, abs=1.0)


def test_refitting_needs_two_separated_points():
    a = (Pin("a", 100, 500), 32.0853, 34.7818)
    with pytest.raises(ValueError):
        scale_from_known_points([a])
    with pytest.raises(ValueError):
        scale_from_known_points([a, (Pin("b", 100, 500), 32.0853, 34.7818)])


# --- pixels to ground -----------------------------------------------------

def test_the_centre_pixel_is_the_centre_of_the_map():
    scale = Scale(m_per_px=10.2)
    lat, lng = to_latlng(Pin("x", 450, 854), TLV, scale)
    assert lat == pytest.approx(TLV[0], abs=1e-6)
    assert lng == pytest.approx(TLV[1], abs=1e-6)


def test_moving_right_increases_longitude_and_down_decreases_latitude():
    """Screen y grows downward while latitude grows northward. Getting this
    backwards puts every venue in the wrong hemisphere of the city."""
    scale = Scale(m_per_px=10.2)
    east = to_latlng(Pin("e", 550, 854), TLV, scale)
    south = to_latlng(Pin("s", 450, 954), TLV, scale)
    assert east[1] > TLV[1]
    assert south[0] < TLV[0]


def test_a_hundred_pixels_is_about_a_kilometre():
    scale = Scale(m_per_px=10.2)
    lat, _lng = to_latlng(Pin("s", 450, 854 + 100), TLV, scale)
    metres = (TLV[0] - lat) * 110_540
    assert metres == pytest.approx(1020, abs=60)


# --- the cell -------------------------------------------------------------

def test_a_viewport_becomes_a_real_rectangle():
    """The map area is about 900x1324 px, so at 10.2 m/px it covers roughly
    9.2 x 13.5 km."""
    cell = cell_for_viewport(TLV, Scale(m_per_px=10.2))
    assert isinstance(cell, Cell)
    width_km = (cell.right - cell.left) * 111.32 * 0.845
    height_km = (cell.top - cell.bottom) * 110.54
    assert width_km == pytest.approx(9.2, abs=1.0)
    assert height_km == pytest.approx(13.5, abs=1.5)


def test_the_cell_is_centred_on_the_search():
    cell = cell_for_viewport(TLV, Scale(m_per_px=10.2))
    assert (cell.left + cell.right) / 2 == pytest.approx(TLV[1], abs=1e-6)
    assert (cell.bottom + cell.top) / 2 == pytest.approx(TLV[0], abs=1e-6)


def test_quartering_a_real_cell_gives_four_real_cells():
    """`Cell.quarters` was already there; this is what makes it mean
    something on the ground rather than in pixels."""
    cell = cell_for_viewport(TLV, Scale(m_per_px=10.2))
    quarters = cell.quarters()
    assert len(quarters) == 4
    assert all(q.right > q.left and q.top > q.bottom for q in quarters)
    # Together they cover the parent exactly.
    assert min(q.left for q in quarters) == cell.left
    assert max(q.right for q in quarters) == cell.right


def test_a_deeper_cell_is_smaller_by_half_in_each_direction():
    cell = cell_for_viewport(TLV, Scale(m_per_px=10.2))
    child = cell.quarters()[0]
    assert (child.right - child.left) == pytest.approx((cell.right - cell.left) / 2)
    assert (child.top - child.bottom) == pytest.approx((cell.top - cell.bottom) / 2)


# --- where the map actually is --------------------------------------------

def test_the_map_centre_must_be_derived_not_assumed():
    """Measured: after searching "Tel Aviv" every known pin sat an almost
    identical 1085 m from where the geocoded centroid predicted -- a constant
    offset, while the scale itself refitted to exactly 10.20 m/px. The app
    centres on its own idea of the place.

    A constant offset is the kindest error available: invisible in the venue
    list, consistent enough to look right, and it would put every cell
    boundary a kilometre from where the coverage report claims."""
    from beer_in_this_town.app_geo import centre_from_known_points

    scale = Scale(m_per_px=10.2)
    truth = (32.0700, 34.7846)
    pins = [Pin("a", 300, 600), Pin("b", 700, 1100)]
    known = [(p, *to_latlng(p, truth, scale)) for p in pins]
    got = centre_from_known_points(known, scale)
    assert got[0] == pytest.approx(truth[0], abs=1e-6)
    assert got[1] == pytest.approx(truth[1], abs=1e-6)


def test_locating_the_map_needs_at_least_one_point():
    from beer_in_this_town.app_geo import centre_from_known_points

    with pytest.raises(ValueError):
        centre_from_known_points([], Scale(m_per_px=10.2))
