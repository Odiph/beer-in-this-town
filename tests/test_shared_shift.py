"""A misplaced sweep cell is not a set of namesakes.

Found live on Tel Aviv, 2026-11-14: a sweep cell is placed as a block, so
when the camera misjudged a pan, every pin in it was off by the same vector.
One cell's six real bars all missed their own pages by 1.6 km, in the same
direction, and enrich refused all six as `too_far`. A namesake in the next
town does not line up with its neighbours like that, which is what tells the
two apart.

No network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.config import Settings, stage_path
from beer_in_this_town.export import write_csv
from beer_in_this_town.flow_cmds import SWEEP_CSV, cmd_enrich
from beer_in_this_town.models import Venue, VenueRef
from beer_in_this_town.resolve import (
    SHIFT_MIN_VENUES,
    resolve_one,
    shared_shifts,
)

pytestmark = pytest.mark.unit

# 1.6 km east, roughly, at Tel Aviv's latitude.
SHIFT_LNG = 0.0170
PIN = (32.0800, 34.7800)


def _near(dlat, dlng):
    return (PIN[0] + dlat, PIN[1] + dlng)


def _cell(n, status="too_far", shift=SHIFT_LNG):
    """`n` venues a few hundred metres apart, each missing by `shift`."""
    out = {}
    for i in range(n):
        pin = _near(0.002 * i, 0.001 * i)
        out[f"c{i}"] = (status, pin, (pin[0], pin[1] + shift))
    return out


def test_a_cell_whose_misses_agree_is_accepted():
    got = shared_shifts(_cell(6))
    assert set(got) == {f"c{i}" for i in range(6)}
    assert "same shift" in got["c0"]


def test_a_lone_miss_is_never_accepted():
    """One venue's offset has nothing to agree with: it may be a namesake."""
    assert shared_shifts(_cell(1)) == {}


def test_too_few_agreeing_misses_are_not_enough():
    assert shared_shifts(_cell(SHIFT_MIN_VENUES - 1)) == {}


def test_matched_neighbours_lift_a_miss_with_the_same_shift():
    """The cell's other venues matched (within 1 km); one missed by a bit
    more. Their shared offset is the evidence."""
    pts = _cell(3, status="resolved", shift=0.0100)  # ~940 m
    pin = _near(0.001, 0.0)
    pts["miss"] = ("too_far", pin, (pin[0], pin[1] + 0.0108))  # ~1.02 km
    assert set(shared_shifts(pts)) == {"miss"}


def test_misses_in_different_directions_are_namesakes():
    pts = {}
    for i, (dlat, dlng) in enumerate([(0.02, 0), (-0.02, 0), (0, 0.02),
                                      (0, -0.02)]):
        pin = _near(0.001 * i, 0)
        pts[f"n{i}"] = ("too_far", pin, (pin[0] + dlat, pin[1] + dlng))
    assert shared_shifts(pts) == {}


def test_agreeing_misses_far_apart_are_not_one_cell():
    pts = {}
    for i in range(4):
        pin = _near(0.15 * i, 0)  # ~17 km apart: not one cell
        pts[f"f{i}"] = ("too_far", pin, (pin[0], pin[1] + SHIFT_LNG))
    assert shared_shifts(pts) == {}


def test_a_shift_beyond_the_ceiling_is_not_a_cell():
    """Thousands of kilometres is a namesake however many agree."""
    assert shared_shifts(_cell(6, shift=0.5)) == {}


def test_too_far_keeps_the_nearest_candidate():
    from beer_in_this_town.app_sweep import Venue as Swept

    def ref(vid):
        return VenueRef(venue_id=vid, slug="x", name="Rosh Pina",
                        category="Bar", address=None, city="Tel Aviv")
    pages = {"1": (40.0, 30.0), "2": (PIN[0], PIN[1] + SHIFT_LNG)}

    def fetch(r):
        lat, lng = pages[r.venue_id]
        return Venue(ref=r, total=1, unique=1, monthly=1, you=None,
                     lat=lat, lng=lng, geo_source="embedded")
    out = resolve_one(Swept("Rosh Pina", 0, 0, *PIN),
                      lambda q: [ref("1"), ref("2")], fetch)
    assert out.status == "too_far"
    assert out.venue is None
    assert out.candidate.ref.venue_id == "2"


# --- through the command, where the decision lands -------------------------

CITY = "Shift Test City"
NAMES = ["Rosh Pina", "Kermeet Bar", "Marla Singer", "Barrio 66", "Beer Stop"]


def _swept(i, name):
    lat, lng = _near(0.002 * i, 0.001 * i)
    return Venue(ref=VenueRef(venue_id="", slug="", name=name, category=None,
                              address=None, city=CITY),
                 total=None, unique=None, monthly=None, you=None,
                 lat=lat, lng=lng, geo_source="app")


def _page(name):
    i = NAMES.index(name)
    lat, lng = _near(0.002 * i, 0.001 * i)
    if name == "Beer Stop":  # a namesake in another country
        lat, lng = 40.0, -75.0
    else:
        lng += SHIFT_LNG
    ref = VenueRef(venue_id=str(i + 1), slug=name.lower(), name=name,
                   category="Bar", address=None, city=CITY)
    return Venue(ref=ref, total=500 + i, unique=50, monthly=5, you=None,
                 lat=lat, lng=lng, geo_source="embedded")


def test_enrich_accepts_a_shifted_cell_and_still_refuses_the_namesake():
    write_csv([_swept(i, n) for i, n in enumerate(NAMES)],
              stage_path(CITY, SWEEP_CSV))
    env = cmd_enrich(Settings(), CITY,
                     search=lambda q: [_page(n).ref for n in NAMES
                                       if n.casefold() in q.casefold()],
                     fetch=lambda r: _page(r.name))
    assert env.ok
    assert env.data["statuses"] == {"resolved": 4, "too_far": 1}
    assert env.data["shifted"] == 4
    import csv
    with open(env.data["csv"], encoding="utf-8-sig") as fh:
        rows = {r["sweep_name"]: r for r in csv.DictReader(fh)}
    assert rows["Rosh Pina"]["resolution"] == "resolved"
    assert rows["Rosh Pina"]["total"] == "500"
    assert rows["Beer Stop"]["resolution"] == "too_far"
    assert rows["Beer Stop"]["total"] == ""
