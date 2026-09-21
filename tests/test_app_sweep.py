"""Sweeping a city out of the app's map, cell by cell.

The rule the whole sweep turns on: a cell that comes back at the result-set
cap was truncated and must be subdivided; one that comes back under it is
taken as complete. Measured on the live app -- two Tel Aviv searches both
returned exactly 58, Haifa returned 37.

The sweep has to run unattended, so every judgement a person made during the
research is an assertion here instead:

- the screen is the map and not the launcher or the list view
- the map actually moved when we asked it to
- a cell that hit the cap gets subdivided rather than accepted
- recursion stops, both on saturation and on a depth bound

`FakeDevice` stands in for adb. No device, no network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.app_map import WrongScreen
from beer_in_this_town.app_sweep import (
    MAP_PACKAGE,
    Cell,
    DeadPan,
    SweepResult,
    sweep,
)

CHROME = """
 <node class="android.view.View" content-desc="Google Map" bounds="[0,192][900,1516]"/>
 <node class="android.widget.Button" content-desc="Switch to list view" bounds="[828,57][876,105]"/>
 <node class="android.widget.Button" content-desc="Refresh search" bounds="[822,282][888,348]"/>
"""


def dump_with(names, offset=0):
    pins = "".join(
        f'<node class="android.view.View" content-desc="{n}." '
        f'bounds="[{100 + offset + i * 7},{200 + i * 9}]'
        f'[{154 + offset + i * 7},{262 + i * 9}]"/>'
        for i, n in enumerate(names))
    return f"<?xml version='1.0'?><hierarchy>{CHROME}{pins}</hierarchy>"


class FakeDevice:
    """Replays a queue of dumps and records what was done to it."""

    def __init__(self, dumps, focus=MAP_PACKAGE):
        self._dumps = list(dumps)
        self.focus = focus
        self.actions: list[str] = []
        self.dumps_served = 0

    def focused_package(self) -> str:
        return self.focus

    def dump(self) -> str:
        self.dumps_served += 1
        if len(self._dumps) > 1:
            return self._dumps.pop(0)
        return self._dumps[0]

    def tap(self, x, y):
        self.actions.append(f"tap({x},{y})")

    def swipe(self, x1, y1, x2, y2, ms):
        self.actions.append(f"swipe({x2 - x1},{y2 - y1})")


CELL = Cell(left=34.74, right=34.84, bottom=32.03, top=32.12)


# --- the happy path -------------------------------------------------------

def test_a_cell_under_the_cap_is_taken_whole():
    dev = FakeDevice([dump_with(["Lauter", "Ursa", "Schnitt"])])
    out = sweep(dev, CELL)
    assert {v.name for v in out.venues} == {"Lauter", "Ursa", "Schnitt"}
    assert out.cells_visited == 1
    assert out.truncated_cells == 0


def test_venues_are_deduplicated_across_cells():
    """Neighbouring cells overlap, so the same venue arrives repeatedly.
    Dedup is load-bearing, not tidying: four children returning the same two
    venues must yield two, not eight."""
    full = dump_with([f"v{i}" for i in range(58)])          # at the cap
    child = dump_with(["Lauter", "Ursa"])                   # every child, same two
    dev = FakeDevice([full] + [child] * 40)
    out = sweep(dev, CELL, max_depth=1)
    names = [v.name for v in out.venues]
    assert len(names) == len(set(names))
    assert names.count("Lauter") == 1
    # The parent's 58 are real venues too -- truncated means incomplete, not
    # wrong, so they are kept rather than thrown away.
    assert len(names) == 60


# --- the saturation rule --------------------------------------------------

def test_a_cell_at_the_cap_is_subdivided():
    full = dump_with([f"v{i}" for i in range(58)])
    child = dump_with(["a", "b"])
    dev = FakeDevice([full] + [child] * 40)
    out = sweep(dev, CELL, max_depth=1)
    assert out.truncated_cells == 1
    assert out.cells_visited == 5          # the parent plus four quadrants
    assert {"a", "b"} <= {v.name for v in out.venues}


def test_an_empty_cell_is_not_subdivided():
    """Zero pins is a real answer about a real place. Treating it as
    truncation recurses forever over open countryside."""
    dev = FakeDevice([dump_with([])])
    out = sweep(dev, CELL, max_depth=3)
    assert out.cells_visited == 1
    assert out.venues == []


def test_recursion_stops_at_the_depth_bound_and_says_so():
    """A dense city centre can stay at the cap however far you divide. The
    sweep must stop and report that it stopped short, not loop."""
    full = dump_with([f"v{i}" for i in range(58)])
    dev = FakeDevice([full])
    out = sweep(dev, CELL, max_depth=2)
    assert out.hit_depth_limit is True
    assert any("depth" in w for w in out.warnings)


def test_a_complete_sweep_does_not_claim_a_depth_problem():
    dev = FakeDevice([dump_with(["one"])])
    assert sweep(dev, CELL).hit_depth_limit is False


# --- the guards -----------------------------------------------------------

def test_the_wrong_app_is_refused_before_anything_is_harvested():
    """A harvest once captured the BlueStacks launcher and reported its five
    nodes as data. Check the focused package first, not the dump."""
    dev = FakeDevice([dump_with(["Lauter"])], focus="com.uncube.launcher3")
    with pytest.raises(WrongScreen, match="launcher3"):
        sweep(dev, CELL)
    assert dev.dumps_served == 0


def test_the_list_view_is_refused():
    listing = "<?xml version='1.0'?><hierarchy>" + \
        '<node class="android.widget.Button" content-desc="Switch to map view"' \
        ' bounds="[828,57][876,105]"/></hierarchy>'
    with pytest.raises(WrongScreen):
        sweep(FakeDevice([listing]), CELL)


def test_a_pan_that_does_not_move_the_map_raises():
    """The map not moving means the gesture is not landing, and every cell
    after it would re-harvest the same place while reporting progress."""
    full = dump_with([f"v{i}" for i in range(58)])
    dev = FakeDevice([full])                    # identical dump forever
    with pytest.raises(DeadPan):
        sweep(dev, CELL, max_depth=1, verify_pans=True)


def test_a_pan_that_moves_is_accepted():
    full = dump_with([f"v{i}" for i in range(58)])
    moved = dump_with([f"v{i}" for i in range(58)], offset=-300)
    dev = FakeDevice([full, moved] + [dump_with(["x"])] * 40)
    out = sweep(dev, CELL, max_depth=1, verify_pans=True)
    assert out.cells_visited == 5


# --- what the caller gets -------------------------------------------------

def test_the_result_records_where_it_looked_and_what_it_doubted():
    full = dump_with([f"v{i}" for i in range(58)])
    dev = FakeDevice([full])
    out = sweep(dev, CELL, max_depth=1)
    assert isinstance(out, SweepResult)
    assert out.cells_visited >= 1
    assert out.truncated_cells >= 1
    assert out.warnings


def test_refresh_is_pressed_for_each_cell():
    """Panning redraws; it does not re-query. Without `Refresh search` every
    child cell shows slices of the parent's result set and the sweep finds
    nothing new."""
    full = dump_with([f"v{i}" for i in range(58)])
    dev = FakeDevice([full] + [dump_with(["a"])] * 40)
    sweep(dev, CELL, max_depth=1)
    # One per child cell. The parent's result set came from the caller's own
    # search, so the sweep does not re-query it.
    assert sum(1 for a in dev.actions if a.startswith("tap")) == 4
