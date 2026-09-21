"""Measuring the map's scale on every run, instead of assuming it.

Found live: after `Reset location` the app chose a different zoom from a city
search, and a sweep converted every pin with the old 10.2 m/px when the real
figure was 8.1. Every venue landed about 600 m from where it is -- all of them
in the same direction, which makes a map that looks entirely plausible.

The fix fits a similarity (scale, rotation, shift) from swept venues whose
real positions OSM knows. Measured on that sweep: 8.12 m/px, -0.2 degrees,
40 m shift, and 18 m median error afterwards.
"""
from __future__ import annotations

import math

import pytest

from beer_in_this_town.app_calibrate import (
    CalibrationFailed,
    calibrate,
    match_known,
    name_keys,
)
from beer_in_this_town.app_sweep import Venue

ORIGIN = (32.075318, 34.808611)
KX = 111_320.0 * math.cos(math.radians(ORIGIN[0]))
KY = 110_540.0


def at(east_m: float, north_m: float) -> tuple[float, float]:
    return ORIGIN[0] + north_m / KY, ORIGIN[1] + east_m / KX


# Where the venues really are, in metres east/north of the origin.
TRUE = {
    "Agnes": (-2800, 700), "Rubina": (-2600, 900), "Little Schnitt": (-2300, -700),
    "Beer Bazaar Habima": (-2500, 1200), "Travitz": (-1200, -400),
    "Porter & Sons": (-900, 1500),
}


def swept(factor: float, shift=(0.0, 0.0), extra=()) -> list[Venue]:
    """What a sweep with a wrong scale reports: true offsets divided by
    `factor`, plus a constant shift."""
    out = []
    for name, (e, n) in list(TRUE.items()) + list(extra):
        lat, lng = at(e / factor + shift[0], n / factor + shift[1])
        out.append(Venue(name=name, x=0, y=0, lat=lat, lng=lng))
    return out


def known(names=TRUE) -> list[tuple[str, float, float]]:
    return [(n, *at(*TRUE[n])) for n in names]


def error_m(v: Venue, true_en) -> float:
    return math.hypot((v.lng - ORIGIN[1]) * KX - true_en[0],
                      (v.lat - ORIGIN[0]) * KY - true_en[1])


# --- matching -------------------------------------------------------------

def test_either_half_of_a_bilingual_name_matches():
    assert name_keys("Agnes (אגנס)") & name_keys("אגנס")
    assert name_keys("Mike's Place (מייקס פלייס)") & name_keys("Mike's Place")


def test_hebrew_names_are_not_all_the_same_name():
    """Stripping to ASCII once collapsed every Hebrew name to "" and matched
    them to each other, inventing kilometre-scale errors."""
    assert not name_keys("שר המשקאות") & name_keys("יינות וטעמים")


def test_short_fragments_do_not_match():
    assert not name_keys("Bar") & name_keys("Bar Kochba")


def test_an_ambiguous_name_is_not_ground_truth():
    """A chain has several branches. Matching it to whichever one OSM listed
    first was a 2 km outlier in the live data."""
    venues = [Venue("Beer Point", 0, 0, *at(0, 0))]
    osm = [("Beer Point", *at(100, 0)), ("Beer Point", *at(5000, 0))]
    assert match_known(venues, osm) == []


def test_venues_without_coordinates_are_not_matched():
    venues = [Venue("Agnes", 0, 0)]
    assert match_known(venues, known(["Agnes"])) == []


# --- fitting --------------------------------------------------------------

def test_recovers_a_wrong_scale():
    venues = swept(factor=0.796)                  # the live measurement
    fixed, cal = calibrate(venues, known(), ORIGIN)
    assert cal.scale == pytest.approx(0.796, rel=0.01)
    for v in fixed:
        assert error_m(v, TRUE[v.name]) < 1.0


def test_recovers_a_shift_as_well():
    fixed, cal = calibrate(swept(1.0, shift=(-300, 200)), known(), ORIGIN)
    for v in fixed:
        assert error_m(v, TRUE[v.name]) < 1.0


def test_corrects_venues_that_had_no_match():
    """The point of the whole thing: the matched venues only measure the
    scale, and every other venue is what it is applied to."""
    extra = [("Unmatched Bar", (-1500, 300))]
    venues = swept(0.8, extra=extra)
    fixed, _ = calibrate(venues, known(), ORIGIN)
    lone = next(v for v in fixed if v.name == "Unmatched Bar")
    assert error_m(lone, (-1500, 300)) < 1.0


def test_an_outlier_is_dropped_and_reported():
    osm = known() + [("Chouffeland", *at(3000, -3000))]
    venues = swept(0.8) + [Venue("Chouffeland", 0, 0, *at(-100, 100))]
    fixed, cal = calibrate(venues, osm, ORIGIN)
    assert "Chouffeland" in cal.dropped
    assert cal.scale == pytest.approx(0.8, rel=0.01)


def test_too_few_matches_refuses():
    """Three points fit anything. A made-up scale would move every venue
    somewhere plausible and wrong."""
    with pytest.raises(CalibrationFailed, match="match"):
        calibrate(swept(0.8), known(["Agnes", "Rubina", "Travitz"]), ORIGIN)


def test_a_fit_that_does_not_explain_the_points_refuses():
    scrambled = [(n, *at(e * -1.3, n_ * 0.4)) for n, (e, n_) in TRUE.items()]
    with pytest.raises(CalibrationFailed):
        calibrate(swept(1.0), scrambled, ORIGIN)


def test_calibration_does_not_modify_its_input():
    venues = swept(0.8)
    before = list(venues)
    calibrate(venues, known(), ORIGIN)
    assert venues == before
