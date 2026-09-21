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


def dump_with(names, offset=0, yoff=0):
    pins = "".join(
        f'<node class="android.view.View" content-desc="{n}." '
        f'bounds="[{100 + offset + i * 7},{200 + yoff + i * 9}]'
        f'[{154 + offset + i * 7},{262 + yoff + i * 9}]"/>'
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

    def type_text(self, text):
        self.actions.append(f"type({text})")

    def press_enter(self):
        self.actions.append("enter")

    def launch(self, package):
        self.actions.append(f"launch({package})")
        self.focus = MAP_PACKAGE

    def swipe(self, x1, y1, x2, y2, ms):
        self.actions.append(f"swipe({x2 - x1},{y2 - y1})")


CELL = Cell(left=34.74, right=34.84, bottom=32.03, top=32.12)


# --- the happy path -------------------------------------------------------

def test_every_cell_re_searches_before_it_harvests():
    """Pins are clipped to the viewport; the cap is a property of the result
    set. A cell that reads a stale, clipped result set undercounts and calls
    itself complete."""
    dev = FakeDevice([dump_with(["Lauter"])])
    sweep(dev, CELL, settle_max_s=0)
    assert dev.actions and dev.actions[0].startswith("tap")


def test_a_cell_under_the_cap_is_taken_whole():
    dev = FakeDevice([dump_with(["Lauter", "Ursa", "Schnitt"])])
    out = sweep(dev, CELL, settle_max_s=0)
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
    out = sweep(dev, CELL, settle_max_s=0, max_depth=1)
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
    out = sweep(dev, CELL, settle_max_s=0, max_depth=1)
    assert out.truncated_cells == 1
    assert out.cells_visited == 5          # the parent plus four quadrants
    assert {"a", "b"} <= {v.name for v in out.venues}


def test_an_empty_cell_is_not_subdivided():
    """Zero pins is a real answer about a real place. Treating it as
    truncation recurses forever over open countryside."""
    dev = FakeDevice([dump_with([])])
    out = sweep(dev, CELL, settle_max_s=0, max_depth=3)
    assert out.cells_visited == 1
    assert out.venues == []


def test_recursion_stops_at_the_depth_bound_and_says_so():
    """A dense city centre can stay at the cap however far you divide. The
    sweep must stop and report that it stopped short, not loop."""
    full = dump_with([f"v{i}" for i in range(58)])
    dev = FakeDevice([full])
    out = sweep(dev, CELL, settle_max_s=0, max_depth=2)
    assert out.hit_depth_limit is True
    assert any("depth" in w for w in out.warnings)


def test_a_complete_sweep_does_not_claim_a_depth_problem():
    dev = FakeDevice([dump_with(["one"])])
    assert sweep(dev, CELL, settle_max_s=0).hit_depth_limit is False


# --- the guards -----------------------------------------------------------

def test_the_wrong_app_is_refused_before_anything_is_harvested():
    """A harvest once captured the BlueStacks launcher and reported its five
    nodes as data. Check the focused package first, not the dump."""
    dev = FakeDevice([dump_with(["Lauter"])], focus="com.uncube.launcher3")
    with pytest.raises(WrongScreen, match="launcher3"):
        sweep(dev, CELL, settle_max_s=0)
    assert dev.dumps_served == 0


def test_the_list_view_is_refused():
    listing = "<?xml version='1.0'?><hierarchy>" + \
        '<node class="android.widget.Button" content-desc="Switch to map view"' \
        ' bounds="[828,57][876,105]"/></hierarchy>'
    with pytest.raises(WrongScreen):
        sweep(FakeDevice([listing]), CELL, settle_max_s=0)


def test_repeated_dead_pans_stop_the_sweep():
    """One dead pan is a cell worth skipping. A run of them means the gesture
    is not landing at all, and every later cell would re-harvest the same
    rectangle while the sweep counted progress."""
    full = dump_with([f"v{i}" for i in range(58)])
    dev = FakeDevice([full])                    # identical dump forever
    with pytest.raises(DeadPan, match="in a row"):
        sweep(dev, CELL, settle_max_s=0, max_depth=1, verify_pans=True)


def test_a_single_dead_pan_skips_its_cell_and_carries_on():
    """Losing a whole sweep to one awkward viewport wastes every cell already
    collected. The skip is recorded so the result never looks complete."""
    full = dump_with([f"v{i}" for i in range(58)])
    stuck = dump_with([f"v{i}" for i in range(58)])      # identical: dead pan
    moved = dump_with([f"v{i}" for i in range(58)], offset=-300)
    dev = FakeDevice([full, stuck, moved] + [dump_with(["ok"])] * 40)
    out = sweep(dev, CELL, settle_max_s=0, max_depth=1, verify_pans=True)
    assert out.skipped_cells == 1
    assert any("skipping" in w for w in out.warnings)
    assert "ok" in {v.name for v in out.venues}


def test_a_stalled_pan_is_only_accused_with_enough_evidence():
    """A large pan legitimately leaves almost no overlap, and the few pins
    that survive are usually clustered markers the map re-lays-out. Firing
    on three shared pins that moved (10,9) px was a false accusation against
    a pan that had worked."""
    full = dump_with([f"v{i}" for i in range(58)])
    # Only three names in common, and those barely move.
    thin = dump_with(["v0", "v1", "v2"] + [f"w{i}" for i in range(55)], offset=3)
    dev = FakeDevice([full, thin] + [dump_with(["x"])] * 40)
    out = sweep(dev, CELL, settle_max_s=0, max_depth=1, verify_pans=True)
    assert out.cells_visited == 5


def test_a_pan_that_moves_is_accepted():
    full = dump_with([f"v{i}" for i in range(58)])
    moved = dump_with([f"v{i}" for i in range(58)], offset=-300)
    dev = FakeDevice([full, moved] + [dump_with(["x"])] * 40)
    out = sweep(dev, CELL, settle_max_s=0, max_depth=1, verify_pans=True)
    assert out.cells_visited == 5


def test_a_pan_starts_away_from_the_markers():
    """Dragging a pin does not move the map. Found live: a pan of (225,331)
    moved the map by (-2,0) because the screen centre had a marker on it."""
    from beer_in_this_town.app_map import Pin
    from beer_in_this_town.app_sweep import _clear_origin

    crowded = [Pin(name=f"p{i}", x=450, y=850 + i) for i in range(5)]
    x, y = _clear_origin(crowded, 225, 331)
    assert (x - 450) ** 2 + (y - 850) ** 2 > 200 ** 2


def test_a_pan_never_starts_on_the_venue_card():
    """The card overlays the bottom of the map and looks like part of it.
    A swipe starting there drags the card while the map stays exactly still --
    found live twice, as a pan of (-225,-331) px that moved the map by (0,0)
    across 58 shared pins."""
    from beer_in_this_town.app_sweep import CARD_TOP, _clear_origin

    for dx, dy in ((-225, -331), (225, 331), (-225, 331), (225, -331)):
        _x, y = _clear_origin([], dx, dy)
        assert y < CARD_TOP


def test_a_pan_origin_leaves_room_for_the_gesture():
    """An origin near the edge would drag the finger off the map area."""
    from beer_in_this_town.app_sweep import (
        MAP_BOTTOM,
        MAP_TOP,
        SCREEN_WIDTH,
        _clear_origin,
    )

    x, y = _clear_origin([], 225, 331)
    assert 0 < x + 225 < SCREEN_WIDTH
    assert MAP_TOP < y + 331 < MAP_BOTTOM


# --- what the caller gets -------------------------------------------------

def test_the_result_records_where_it_looked_and_what_it_doubted():
    full = dump_with([f"v{i}" for i in range(58)])
    dev = FakeDevice([full])
    out = sweep(dev, CELL, settle_max_s=0, max_depth=1)
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
    sweep(dev, CELL, settle_max_s=0, max_depth=1)
    # One per cell, the root included. Found the hard way: the first live
    # sweep inherited a panned map, saw 56 of a 58-venue result set, and
    # concluded the cell was complete. A cell must re-search before it
    # believes its own pin count.
    assert sum(1 for a in dev.actions if a.startswith("tap")) == 5


# --- the category filter --------------------------------------------------

def _panel(rows):
    nodes = "".join(
        f'<node class="android.widget.CheckBox" content-desc="{n}, {c}, " '
        f'checked="{str(k).lower()}" bounds="[0,{254 + i * 82}][900,{336 + i * 82}]"/>'
        for i, (n, c, k) in enumerate(rows))
    return ("<?xml version='1.0'?><hierarchy>"
            '<node class="android.widget.TextView" text="Filter by Category"'
            ' bounds="[132,58][370,99]"/>' + nodes + "</hierarchy>")


def test_the_filter_turns_drinking_categories_on_and_junk_off():
    """Only 6 of the 17 categories a Tel Aviv search returns are drinking
    places, so most of a 58-slot result set goes to parks and hotels. It
    cannot be fixed afterwards: pins carry no category."""
    from beer_in_this_town.app_sweep import apply_drinking_filter

    settled = _panel([("Bar", 6, True), ("Supermarket", 2, False)])
    dev = FakeDevice([_panel([("Bar", 6, False), ("Supermarket", 2, True)]), settled])
    kept = apply_drinking_filter(dev, settle_max_s=0)
    assert kept == ["Bar"]
    # Two toggles, plus filters / category row / APPLY / SHOW RESULTS.
    assert sum(1 for a in dev.actions if a.startswith("tap")) >= 6


def test_the_filter_stops_when_the_panel_stops_changing():
    """The panel scrolls, so the driver must know when it has reached the
    bottom rather than scrolling forever."""
    from beer_in_this_town.app_sweep import apply_drinking_filter

    dev = FakeDevice([_panel([("Bar", 6, True)])])
    apply_drinking_filter(dev, settle_max_s=0)
    assert sum(1 for a in dev.actions if a.startswith("swipe")) <= 1


# --- navigation guards ----------------------------------------------------

def test_navigation_refuses_to_act_on_the_wrong_screen():
    """`require_map_screen` protects the harvest; this protects the
    navigation. A measurement run once failed only *after* its search taps had
    landed, because the app was on a venue page -- so a city name was typed
    into whatever was focused."""
    from beer_in_this_town.app_sweep import ensure_map_screen

    venue = "<?xml version='1.0'?><hierarchy><node class='android.widget.TextView'"             " text='Venue' bounds='[1,1][2,2]'/></hierarchy>"
    dev = FakeDevice([venue])
    with pytest.raises(WrongScreen):
        ensure_map_screen(dev, relaunch=False, settle_max_s=0)


def test_navigation_relaunches_rather_than_pressing_back():
    """Backing out of an unexpected screen emptied the app to the BlueStacks
    launcher twice during the research. Relaunching is deterministic."""
    from beer_in_this_town.app_sweep import ensure_map_screen

    # The focus check fails before any dump is read, so the queue only needs
    # what the app shows once it has been relaunched.
    dev = FakeDevice([dump_with(["Lauter"])], focus="com.uncube.launcher3")
    ensure_map_screen(dev, settle_max_s=0)
    assert any(a.startswith("launch(") for a in dev.actions)
    assert not any("keyevent" in a for a in dev.actions)


def test_a_city_search_checks_the_screen_before_typing():
    from beer_in_this_town.app_sweep import search_city

    dev = FakeDevice([dump_with(["Lauter"])])
    search_city(dev, "Tel Aviv", settle_max_s=0)
    order = [a for a in dev.actions if a.startswith(("tap", "type", "enter"))]
    assert "type(Tel Aviv)" in order
    assert order.index("type(Tel Aviv)") < order.index("enter")


# --- the resume journal ---------------------------------------------------

def test_a_sweep_resumes_from_its_journal(tmp_path, monkeypatch):
    """A sweep is minutes of real gestures against a real account. Losing it
    to a crash on the nineteenth cell is expensive."""
    from beer_in_this_town import app_sweep

    monkeypatch.setattr(app_sweep, "STATE_DIR", tmp_path)
    app_sweep.save_journal("Tel Aviv", app_sweep.SweepResult(
        venues=[app_sweep.Venue("Lauter", 1, 2)], cells_visited=3))

    dev = FakeDevice([dump_with(["Ursa"])])
    out = sweep(dev, CELL, settle_max_s=0, city="Tel Aviv")
    assert {v.name for v in out.venues} == {"Lauter", "Ursa"}
    assert out.cells_visited == 4


def test_the_journal_is_written_after_every_cell(tmp_path, monkeypatch):
    """Writing once at the end makes the journal useless for the only case
    it exists to serve."""
    from beer_in_this_town import app_sweep

    monkeypatch.setattr(app_sweep, "STATE_DIR", tmp_path)
    dev = FakeDevice([dump_with(["Lauter"])])
    sweep(dev, CELL, settle_max_s=0, city="Tel Aviv")
    assert app_sweep.journal_path("Tel Aviv").exists()
    assert "Lauter" in app_sweep.journal_path("Tel Aviv").read_text(encoding="utf-8")


def test_journals_are_scoped_per_city(tmp_path, monkeypatch):
    """An unscoped journal would let a Haifa sweep resume into a Tel Aviv
    corpus, the same trap `pinned_<list>.json` is scoped against."""
    from beer_in_this_town import app_sweep

    monkeypatch.setattr(app_sweep, "STATE_DIR", tmp_path)
    assert app_sweep.journal_path("Tel Aviv") != app_sweep.journal_path("Haifa")


def test_an_unreadable_journal_starts_fresh_rather_than_crashing(tmp_path, monkeypatch):
    from beer_in_this_town import app_sweep

    monkeypatch.setattr(app_sweep, "STATE_DIR", tmp_path)
    app_sweep.journal_path("Tel Aviv").write_text("{ broken", encoding="utf-8")
    assert app_sweep.load_journal("Tel Aviv").venues == []


def test_a_filtered_sweep_refilters_every_cell():
    """`Refresh search` clears the category filter -- measured: 9 categories
    checked before it, all 17 after. A sweep that filtered once and then
    refreshed per cell came back entirely unfiltered, with parks and hotels
    in the output and zero new venues."""
    from beer_in_this_town import app_sweep

    panel = ("<?xml version='1.0'?><hierarchy>"
             '<node class="android.widget.TextView" text="Filter by Category"'
             ' bounds="[132,58][370,99]"/>'
             '<node class="android.widget.CheckBox" content-desc="Bar, 6, "'
             ' checked="true" bounds="[0,254][900,336]"/></hierarchy>')
    # One panel dump per pass: the driver re-reads after scrolling.
    dev = FakeDevice([panel, panel, dump_with(["Lauter"])])
    sweep(dev, CELL, settle_max_s=0, filter_drinking=True)
    taps = [a for a in dev.actions if a.startswith("tap")]
    assert f"tap({app_sweep.FILTERS_BUTTON[0]},{app_sweep.FILTERS_BUTTON[1]})" in taps


def test_an_unfiltered_sweep_still_just_refreshes():
    from beer_in_this_town import app_sweep

    dev = FakeDevice([dump_with(["Lauter"])])
    sweep(dev, CELL, settle_max_s=0)
    taps = [a for a in dev.actions if a.startswith("tap")]
    assert taps == [f"tap({app_sweep.REFRESH_BUTTON[0]},{app_sweep.REFRESH_BUTTON[1]})"]


# --- overlap at the seams -------------------------------------------------

def test_children_overlap_rather_than_tile():
    """A seam is where venues go missing: the pan is not pixel-exact, a
    marker on the boundary may not render, and dense markers are drawn
    displaced by up to ~100 px."""
    from beer_in_this_town.app_sweep import CELL_OVERLAP

    parent = Cell(left=34.0, right=35.0, bottom=32.0, top=33.0)
    tl, tr, bl, br = parent.quarters()
    assert tl.right > tr.left          # they share ground, not an edge
    assert tl.bottom < tr.top
    assert CELL_OVERLAP > 0


def test_the_overlap_is_a_margin_not_a_doubling():
    """Too much overlap re-harvests the same ground at full cost."""
    from beer_in_this_town.app_sweep import CELL_OVERLAP

    assert 0 < CELL_OVERLAP < 0.5
    parent = Cell(left=34.0, right=35.0, bottom=32.0, top=33.0)
    child = parent.quarters()[0]
    width = child.right - child.left
    assert width < (parent.right - parent.left) * 0.75


def test_children_still_cover_the_whole_parent():
    parent = Cell(left=34.0, right=35.0, bottom=32.0, top=33.0)
    quarters = parent.quarters()
    assert min(q.left for q in quarters) == parent.left
    assert max(q.right for q in quarters) == parent.right
    assert min(q.bottom for q in quarters) == parent.bottom
    assert max(q.top for q in quarters) == parent.top


def test_the_pan_step_is_short_of_a_quarter():
    """The geometry overlapping is no use if the gesture still moves a full
    quarter -- the viewports would tile even though the cells do not."""
    from beer_in_this_town.app_sweep import (
        CELL_OVERLAP,
        MAP_BOTTOM,
        MAP_TOP,
        SCREEN_WIDTH,
    )

    step_x = int(SCREEN_WIDTH // 4 * (1 - CELL_OVERLAP))
    step_y = int((MAP_BOTTOM - MAP_TOP) // 4 * (1 - CELL_OVERLAP))
    assert step_x < SCREEN_WIDTH // 4
    assert step_y < (MAP_BOTTOM - MAP_TOP) // 4


def test_a_pan_that_lands_off_target_is_corrected():
    """The fling overshoots by up to 27%, and that error was measured and
    then ignored -- so it accumulated across a sweep and shifted every later
    cell off its target."""
    full = dump_with([f"v{i}" for i in range(58)])
    short = dump_with([f"v{i}" for i in range(58)], offset=-60)   # asked ~191
    dev = FakeDevice([full, short] + [dump_with(["x"])] * 40)
    sweep(dev, CELL, settle_max_s=0, max_depth=1, verify_pans=True)
    swipes = [a for a in dev.actions if a.startswith("swipe")]
    assert len(swipes) > 4          # four cells plus at least one correction


def test_a_pan_within_tolerance_is_left_alone():
    """Correcting a small drift costs more than the drift, and the cells
    overlap by ~34 px, which absorbs it."""
    from beer_in_this_town.app_sweep import (
        CELL_OVERLAP,
        MAP_BOTTOM,
        MAP_TOP,
        SCREEN_WIDTH,
    )

    step_x = int(SCREEN_WIDTH // 4 * (1 - CELL_OVERLAP))
    step_y = int((MAP_BOTTOM - MAP_TOP) // 4 * (1 - CELL_OVERLAP))
    full = dump_with([f"v{i}" for i in range(58)])
    # Both axes must land close; a fixture that moves only x reads as a
    # 281 px shortfall in y and correctly triggers a correction.
    close = dump_with([f"v{i}" for i in range(58)],
                      offset=-step_x - 5, yoff=-step_y - 5)
    dev = FakeDevice([full, close] + [dump_with(["x"])] * 40)
    sweep(dev, CELL, settle_max_s=0, max_depth=1, verify_pans=True)
    assert len([a for a in dev.actions if a.startswith("swipe")]) == 4


# --- a sparse wide cell is not a complete one -----------------------------

def test_a_cell_under_the_cap_still_splits_when_min_depth_demands_it():
    """Singapore returned 13 venues, was under the cap, declared itself
    finished -- and matched 5 of the 100 venues in a known top-100 corpus.
    A city-state search lands the map at country zoom, where few pins over a
    huge area read exactly like few pins over a small one."""
    dev = FakeDevice([dump_with(["a", "b"])])
    out = sweep(dev, CELL, settle_max_s=0, min_depth=1, max_depth=2)
    assert out.cells_visited == 5          # split despite being under the cap


def test_min_depth_does_not_inflate_the_truncated_count():
    """A cell split for being wide was not hiding venues behind the cap, and
    reporting it as truncated would overstate what was missed."""
    dev = FakeDevice([dump_with(["a"])])
    out = sweep(dev, CELL, settle_max_s=0, min_depth=1, max_depth=2)
    assert out.truncated_cells == 0


def test_min_depth_zero_keeps_the_old_behaviour():
    dev = FakeDevice([dump_with(["a"])])
    assert sweep(dev, CELL, settle_max_s=0).cells_visited == 1
