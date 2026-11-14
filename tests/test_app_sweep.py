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

from functools import partial

import pytest

from beer_in_this_town.app_map import WrongScreen
from beer_in_this_town.app_sweep import (
    MAP_PACKAGE,
    Cell,
    DeadPan,
    SweepResult,
)
from beer_in_this_town.app_sweep import sweep as library_sweep

from .untappd5 import ScreenApp

# The library defaults are the measured ones (min_depth 1, filter on). Most
# tests here exercise the recursion and the harvest in isolation, so they opt
# out explicitly; the tests below that check the defaults use library_sweep.
sweep = partial(library_sweep, min_depth=0, filter_drinking=False)

CHROME = """
 <node class="android.view.View" content-desc="Google Map" bounds="[0,192][900,1516]"/>
 <node class="android.widget.Button" content-desc="Switch to list view" bounds="[828,57][876,105]"/>
 <node class="android.widget.Button" content-desc="Filter" bounds="[738,36][804,102]"/>
 <node class="android.widget.Button" content-desc="Tel Aviv-Yafo, Israel" bounds="[18,36][429,102]"/>
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

def test_an_unfiltered_cell_is_harvested_without_tapping_anything():
    """Untappd 5.x has no `Refresh search`: the city search has already
    queried the root viewport, so there is nothing to press. A tap here would
    land on whatever sits where the 4.x button used to be."""
    dev = FakeDevice([dump_with(["Lauter"])])
    out = sweep(dev, CELL, settle_max_s=0)
    assert not any(a.startswith("tap") for a in dev.actions), dev.actions
    assert {v.name for v in out.venues} == {"Lauter"}


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


def test_a_child_cell_is_reached_by_panning_alone():
    """In Untappd 5.x a pan re-queries by itself. Measured 2026-11-14: four
    quarter-screen pans over filtered Tel Aviv saw 71 distinct venues, more
    than one result set holds, and each view's pins were final within 4 s.
    So a child cell is a pan and a settle, with no tap at all."""
    full = dump_with([f"v{i}" for i in range(58)])
    dev = FakeDevice([full] + [dump_with(["a"])] * 40)
    sweep(dev, CELL, settle_max_s=0, max_depth=1)
    # Four children, then one pan handing the map back to the parent.
    assert sum(1 for a in dev.actions if a.startswith("swipe")) == 5
    assert not any(a.startswith("tap") for a in dev.actions), dev.actions


# --- the category filter --------------------------------------------------

BEER = [("Bar", 21, True), ("Liquor Store", 7, True), ("Pub", 7, True)]
JUNK = [("Park", 7, True), ("Hotel", 6, True), ("Supermarket", 2, True)]


def test_the_filter_keeps_only_drinking_categories():
    """Only a third of the categories a Tel Aviv search returns are places you
    drink, so most of a 58-slot result set goes to parks and hotels. It cannot
    be fixed afterwards: pins carry no category."""
    from beer_in_this_town.app_sweep import apply_drinking_filter

    app = ScreenApp(categories=BEER + JUNK)
    kept = apply_drinking_filter(app, settle_max_s=0)
    assert sorted(app.applied) == ["Bar", "Liquor Store", "Pub"]
    assert kept == ["Bar", "Liquor Store", "Pub"]
    assert app.screen == "map", "Show Results should land back on the map"


def test_the_filter_starts_from_deselect_all():
    """Every category arrives ticked. `Deselect All` turns them off in one
    tap; toggling seventy rows off one by one is seventy chances to miss."""
    from beer_in_this_town.app_sweep import apply_drinking_filter

    app = ScreenApp(categories=BEER + JUNK)
    apply_drinking_filter(app, settle_max_s=0)
    taps = [a for a in app.actions if a.startswith("tap:")]
    assert taps.index("tap:Deselect All") < taps.index("tap:Bar, 21")
    assert "tap:Park, 7" not in taps


def test_the_filter_does_not_select_all_when_the_list_opens_cleared():
    """A list left cleared by an earlier run opens showing `Select All`.
    Pressing the bulk button then would tick every category -- the opposite
    of what the pass assumes."""
    from beer_in_this_town.app_sweep import apply_drinking_filter

    cleared = [(n, c, False) for n, c, _ in BEER + JUNK]
    app = ScreenApp(categories=cleared)
    apply_drinking_filter(app, settle_max_s=0)
    assert "tap:Select All" not in app.actions
    assert sorted(app.applied) == ["Bar", "Liquor Store", "Pub"]


def test_the_filter_reaches_rows_below_the_fold_and_then_stops():
    """The list scrolls. A drinking category on the second page must still be
    ticked, and the driver must notice the bottom rather than scroll forever."""
    from beer_in_this_town.app_sweep import MAX_CATEGORY_PASSES, apply_drinking_filter

    rows = [(f"Junk {i}", 1, True) for i in range(30)] + [("Brewery", 3, True)]
    app = ScreenApp(categories=rows)
    apply_drinking_filter(app, settle_max_s=0)
    assert app.applied == ["Brewery"]
    assert sum(1 for a in app.actions if a.startswith("swipe")) < MAX_CATEGORY_PASSES


def test_the_filter_refuses_when_its_button_is_not_on_screen():
    """Every control is found by its label. If `Filter` is not there, the map
    is not in front of us, and a tap at its old position hits something else."""
    from beer_in_this_town.app_sweep import apply_drinking_filter

    app = ScreenApp(screen="elsewhere")
    with pytest.raises(WrongScreen, match="Filter"):
        apply_drinking_filter(app, settle_max_s=0)
    assert not any(a.startswith("tap") for a in app.actions)


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


def test_a_city_search_goes_through_the_location_sheet():
    """Untappd 5.x moved the search box into a "Choose Location" sheet behind
    the place name at the top of the map. Enter runs the search."""
    from beer_in_this_town.app_sweep import search_city

    app = ScreenApp(place="Haifa, Israel")
    search_city(app, "Tel Aviv", settle_max_s=0)
    assert app.searched == ["Tel Aviv"]
    assert app.screen == "map"
    order = app.actions
    assert order.index("tap:Haifa, Israel") < order.index("type:Tel Aviv") < order.index("enter")


def test_a_city_search_clears_text_left_in_the_box():
    """The sheet can reopen with the last query still typed. Typing after it
    would search for `HaifaTel Aviv`."""
    from beer_in_this_town.app_sweep import search_city

    app = ScreenApp(typed="Haifa")
    search_city(app, "Tel Aviv", settle_max_s=0)
    assert app.searched == ["Tel Aviv"]
    assert "tap:Clear" in app.actions


def test_a_city_search_never_types_unless_the_sheet_opened():
    """The sheet is drawn over the map and the map's nodes stay in the dump,
    so the map check alone cannot tell it is open. If `Choose Location` is not
    there, typing would go into whatever has focus."""
    from beer_in_this_town.app_sweep import search_city

    app = ScreenApp()
    app._on_tap = lambda label: None          # the location button does nothing
    with pytest.raises(WrongScreen, match="Choose Location"):
        search_city(app, "Tel Aviv", settle_max_s=0)
    assert not any(a.startswith("type") for a in app.actions)


def test_a_search_that_leaves_the_sheet_open_is_not_a_city():
    """Enter on a name the app cannot place leaves the sheet up. Harvesting
    then would read the map underneath -- the previous city -- as this one."""
    from beer_in_this_town.app_sweep import search_city

    app = ScreenApp()
    app.press_enter = lambda: app.actions.append("enter")   # nothing happens
    with pytest.raises(WrongScreen, match="Choose Location"):
        search_city(app, "Nowhere", settle_max_s=0)


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


def test_a_filtered_sweep_filters_once_not_per_cell():
    """In Untappd 5.x the filter survives the automatic re-query. Measured
    2026-11-14: filtered Tel Aviv, panned away and back, came back with 52
    pins and still no park, market or hotel. (4.x was the opposite: its
    `Refresh search` cleared the filter, so every cell had to refilter.)"""
    app = ScreenApp(pins=tuple(f"v{i}" for i in range(58)), categories=BEER + JUNK)
    sweep(app, CELL, settle_max_s=0, max_depth=1, filter_drinking=True)
    assert app.actions.count("tap:Filter") == 1
    assert sum(1 for a in app.actions if a.startswith("swipe(")) >= 4


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
    # Four children and the hand-back; no corrective pan among them.
    assert len([a for a in dev.actions if a.startswith("swipe")]) == 5


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


def test_library_defaults_are_the_measured_ones():
    """min_depth 1, max_depth 3, filter on -- the same as the CLI and the
    census. A library default that differs is a second, unmeasured setting."""
    import inspect

    from beer_in_this_town import app_pipeline

    params = inspect.signature(library_sweep).parameters
    assert params["min_depth"].default == app_pipeline.DEFAULT_MIN_DEPTH == 1
    assert params["max_depth"].default == app_pipeline.DEFAULT_MAX_DEPTH == 3
    assert params["filter_drinking"].default is True


# --- venues that know where they are --------------------------------------
#
# Without a camera a `Venue` carries the screen pixels of whichever dump
# happened to produce it, which stop meaning anything the moment the map
# pans. With one, the conversion happens while that viewport is still the
# live one.

def test_without_a_camera_a_venue_has_no_coordinates():
    """No camera must mean no coordinates, never plausible ones.

    A fabricated lat/lng is the failure this whole module exists to avoid:
    it puts every venue somewhere real-looking and wrong.
    """
    dev = FakeDevice([dump_with(["Lauter", "Schnitt"])])
    result = sweep(dev, CELL, max_depth=0, settle_min_s=0, settle_max_s=0)
    assert [v.name for v in result.venues] == ["Lauter", "Schnitt"]
    assert all(v.lat is None and v.lng is None for v in result.venues)


def test_a_camera_gives_every_venue_a_coordinate():
    from beer_in_this_town.app_geo import Camera, Scale

    cam = Camera(centre=(32.0853, 34.7818), scale=Scale(m_per_px=10.2))
    dev = FakeDevice([dump_with(["Lauter", "Schnitt"])])
    result = sweep(dev, CELL, max_depth=0, camera=cam,
                   settle_min_s=0, settle_max_s=0)
    assert all(v.lat is not None and v.lng is not None for v in result.venues)
    # Near the searched city, not somewhere plausible on another continent.
    for v in result.venues:
        assert abs(v.lat - 32.0853) < 0.2
        assert abs(v.lng - 34.7818) < 0.2


def test_the_camera_follows_the_sweep_as_it_pans():
    """Two cells must not report the same ground.

    The sweep pans a quarter-viewport between children. If the camera does
    not move with it, every cell converts its pins against the root centre
    and the whole city collapses onto one rectangle -- with the right number
    of venues, and all of them in the wrong place.
    """
    from beer_in_this_town.app_geo import Camera, Scale

    cam = Camera(centre=(32.0853, 34.7818), scale=Scale(m_per_px=10.2))
    start = cam.centre
    full = [f"V{i}" for i in range(60)]  # at the cap, so it subdivides
    dev = FakeDevice([dump_with(full), dump_with(["Child"])])
    sweep(dev, CELL, max_depth=1, camera=cam,
          settle_min_s=0, settle_max_s=0)
    assert cam.centre != start, "the camera never moved with the viewport"


# --- recovering to the map from a cold start -------------------------------
#
# Found live: `launch` resumed the running app on the category filter panel
# a crashed sweep had left open, and recovery then tapped `VIEW_MAP_ROW`'s
# remembered coordinate on that panel -- a blind tap on an unknown screen,
# landing on a checkbox. The fix has two halves: the app really restarts
# (see test_adb_device), and the `Map` button is found, not assumed.

# Untappd 5.x's Discover: the way to the map is a `Map` button, not 4.x's
# "View Map" row.
DISCOVER = ("<?xml version='1.0'?><hierarchy>"
            "<node class='android.widget.Button' content-desc='Discover' "
            "bounds='[180,1516][360,1600]'/>"
            "<node class='android.widget.Button' content-desc='Map' "
            "bounds='[389,1425][512,1509]'/>"
            "</hierarchy>")

FILTER_PANEL = ("<?xml version='1.0'?><hierarchy>"
                "<node class='android.widget.CheckBox' "
                "content-desc='Beer Garden, 2' bounds='[0,360][900,420]'/>"
                "</hierarchy>")


def test_relaunch_taps_the_map_button_where_it_actually_is():
    from beer_in_this_town.app_sweep import ensure_map_screen

    dev = FakeDevice([DISCOVER, dump_with(["Lauter"])],
                     focus="com.uncube.launcher3")
    ensure_map_screen(dev, settle_max_s=0)
    assert "tap(450,1467)" in dev.actions, dev.actions


def test_relaunch_onto_an_unknown_screen_raises_without_tapping():
    """No `Map` button, no tap. A guess here lands on whatever is under it."""
    from beer_in_this_town.app_sweep import ensure_map_screen

    dev = FakeDevice([FILTER_PANEL], focus="com.uncube.launcher3")
    with pytest.raises(WrongScreen, match="'Map'"):
        ensure_map_screen(dev, settle_max_s=0)
    assert not any(a.startswith("tap(") for a in dev.actions), dev.actions


def test_relaunch_waits_for_a_cold_start_instead_of_giving_up():
    """Found live: a force-stopped app was still loading when the one dump
    after relaunch was taken, and recovery refused a screen that became
    Discover a few seconds later. Waiting is the fix; guessing is not."""
    from beer_in_this_town.app_sweep import ensure_map_screen

    splash = "<?xml version='1.0'?><hierarchy><node text='' bounds='[0,0][9,9]'/></hierarchy>"
    dev = FakeDevice([splash, splash, DISCOVER, dump_with(["Lauter"])],
                     focus="com.uncube.launcher3")
    ensure_map_screen(dev, settle_max_s=0)
    assert "tap(450,1467)" in dev.actions, dev.actions


def test_relaunch_waits_through_a_window_less_cold_start():
    """Found live on Untappd 5.0.3: straight after the relaunch, uiautomator
    answered `null root node` -- the app had no window yet -- and the sweep
    stopped with `adb_unavailable`. A cold start is exactly when that is
    expected, so recovery keeps polling through it."""
    from beer_in_this_town.adb_device import ScreenNotReady
    from beer_in_this_town.app_sweep import ensure_map_screen

    class ColdStart(FakeDevice):
        def __init__(self, *a, not_ready=2, **k):
            super().__init__(*a, **k)
            self.not_ready = not_ready

        def dump(self):
            if self.focus == MAP_PACKAGE and self.not_ready and any(
                    a.startswith("launch(") for a in self.actions):
                self.not_ready -= 1
                raise ScreenNotReady("null root node")
            return super().dump()

    dev = ColdStart([DISCOVER, dump_with(["Lauter"])], focus="com.uncube.launcher3")
    ensure_map_screen(dev, settle_max_s=0)
    assert "tap(450,1467)" in dev.actions, dev.actions


def test_relaunch_does_not_wait_forever_for_a_window():
    from beer_in_this_town.adb_device import ScreenNotReady
    from beer_in_this_town.app_sweep import ensure_map_screen

    class NeverReady(FakeDevice):
        def dump(self):
            if any(a.startswith("launch(") for a in self.actions):
                self.dumps_served += 1
                raise ScreenNotReady("null root node")
            return super().dump()

    dev = NeverReady([DISCOVER], focus="com.uncube.launcher3")
    with pytest.raises(ScreenNotReady):
        ensure_map_screen(dev, settle_max_s=0)


def test_relaunch_waiting_is_bounded():
    from beer_in_this_town.app_sweep import RELAUNCH_POLLS, ensure_map_screen

    dev = FakeDevice([FILTER_PANEL], focus="com.uncube.launcher3")
    with pytest.raises(WrongScreen):
        ensure_map_screen(dev, settle_max_s=0)
    assert dev.dumps_served <= RELAUNCH_POLLS + 1


def test_each_venue_records_the_cell_that_found_it(tmp_path, monkeypatch):
    """Placement is corrected per cell, so a venue must know its cell --
    through the journal too, or a resumed sweep loses it."""
    from beer_in_this_town import app_sweep

    monkeypatch.setattr(app_sweep, "STATE_DIR", tmp_path)
    out = sweep(FakeDevice([dump_with(["Lauter", "Ursa"])]), CELL,
                settle_max_s=0, city="Tel Aviv")
    assert {v.cell for v in out.venues} == {1}
    assert {v.cell for v in app_sweep.load_journal("Tel Aviv").venues} == {1}


# --- a map that really moves: pins sit at fixed ground positions ------------
#
# Found live on Untappd 5.x (Tel Aviv, depth 1): every second child pan
# "landed (193,261) px off target", the third was skipped as a dead pan
# though the map had moved, and one cell was dead-reckoned 5.8 km off. The
# swipes are relative to wherever the map is, but each was measured against
# the *parent's* screen, captured once. The queue-of-dumps fake above cannot
# show that: its screens do not depend on where the map is.

class WorldDevice:
    """A map whose content sits at fixed positions and moves when swiped.

    `fling` scales every drag, the way a fast `input swipe` overshoots.
    The screen shows at most `cap` pins, the most popular first, like the
    app's result-set cap.
    """

    def __init__(self, spacing=45, fling=1.0, cap=58, blind=()):
        from beer_in_this_town.app_sweep import MAP_BOTTOM, MAP_TOP, SCREEN_WIDTH
        self.top, self.bottom, self.width = MAP_TOP, MAP_BOTTOM, SCREEN_WIDTH
        self.pins = [(f"W{x}_{y}", x, y)
                     for x in range(-1800, 2700, spacing)
                     for y in range(-1800, 3400, spacing)]
        self.fling, self.cap = fling, cap
        import random
        order = list(range(len(self.pins)))
        random.Random(7).shuffle(order)
        self.rank = {self.pins[i][0]: r for r, i in enumerate(order)}
        self.ox = self.oy = 0.0
        self.offsets: list[tuple[float, float]] = []
        self.focus = MAP_PACKAGE
        # Swipe numbers (1-based) after which the re-query shows only venues
        # the previous screen did not: nothing shared, so the pan cannot be
        # measured. What a 5.x re-query that reshuffles its top results does.
        self.blind, self.swipes = set(blind), 0
        self._last: set[str] = set()
        self._blind_next = False

    def focused_package(self):
        return self.focus

    def _visible(self):
        on = [(n, x + self.ox, y + self.oy) for n, x, y in self.pins
              if 30 <= x + self.ox <= self.width - 30
              and self.top + 70 <= y + self.oy <= self.bottom]
        # Untappd returns its most popular venues across the whole view, not
        # the nearest ones, so a quarter-screen pan shares many of them.
        on.sort(key=lambda p: self.rank[p[0]])
        if self._blind_next:
            on = [p for p in on if p[0] not in self._last]
            self._blind_next = False
        on = on[:self.cap]
        self._last = {p[0] for p in on}
        return on

    def dump(self):
        self.offsets.append((self.ox, self.oy))
        nodes = "".join(
            f'<node class="android.view.View" content-desc="{n}" '
            f'bounds="[{int(x) - 27},{int(y) - 62}][{int(x) + 27},{int(y)}]"/>'
            for n, x, y in self._visible())
        return f"<?xml version='1.0'?><hierarchy>{CHROME}{nodes}</hierarchy>"

    def swipe(self, x1, y1, x2, y2, ms):
        self.ox += (x2 - x1) * self.fling
        self.oy += (y2 - y1) * self.fling
        self.swipes += 1
        self._blind_next = self.swipes in self.blind

    def tap(self, x, y):
        pass


def _child_targets():
    from beer_in_this_town.app_sweep import (
        CELL_OVERLAP,
        MAP_BOTTOM,
        MAP_TOP,
        SCREEN_WIDTH,
    )
    sx = int(SCREEN_WIDTH // 4 * (1 - CELL_OVERLAP))
    sy = int((MAP_BOTTOM - MAP_TOP) // 4 * (1 - CELL_OVERLAP))
    return [(-sx, -sy), (sx, -sy), (-sx, sy), (sx, sy)]


def _visited(dev, target, tol=26):
    return any(abs(ox - target[0]) <= tol and abs(oy - target[1]) <= tol
               for ox, oy in dev.offsets)


@pytest.mark.parametrize("fling", [1.0, 1.15])
def test_every_child_is_visited_where_it_should_be(fling):
    dev = WorldDevice(fling=fling)
    result = sweep(dev, CELL, max_depth=1, verify_pans=True,
                   settle_min_s=0, settle_max_s=0)
    missed = [t for t in _child_targets() if not _visited(dev, t)]
    assert not missed, f"children never reached: {missed}"
    assert result.skipped_cells == 0, result.warnings


def test_a_sweep_leaves_the_map_where_it_found_it():
    """What lets the recursion compose: a parent can only aim at its next
    child if the last one handed the map back where it started."""
    dev = WorldDevice(fling=1.15)
    sweep(dev, CELL, max_depth=2, verify_pans=True,
          settle_min_s=0, settle_max_s=0)
    assert abs(dev.ox) <= 26 and abs(dev.oy) <= 26, (dev.ox, dev.oy)


def test_the_camera_tracks_where_the_map_really_is():
    """Each pan is measured against the screen just before it, so the camera
    adds up real moves. Measured against a stale screen, it counted every
    earlier child's move again and drifted kilometres."""
    from beer_in_this_town.app_geo import Camera, Scale

    cam = Camera(centre=(32.0853, 34.7818), scale=Scale(m_per_px=10.0))
    dev = WorldDevice(fling=1.15)
    result = sweep(dev, CELL, max_depth=2, verify_pans=True, camera=cam,
                   settle_min_s=0, settle_max_s=0)
    truth = Camera(centre=(32.0853, 34.7818), scale=Scale(m_per_px=10.0))
    truth.pan_px(dev.ox, dev.oy)
    assert cam.centre == pytest.approx(truth.centre, abs=3e-4)  # ~30 m
    # And every venue sits on its ground position, whichever cell found it.
    ground = {n: (x, y) for n, x, y in dev.pins}
    ref = Camera(centre=(32.0853, 34.7818), scale=Scale(m_per_px=10.0))
    from beer_in_this_town.app_map import Pin
    worst = max(
        max(abs(v.lat - ref.locate(Pin(v.name, *ground[v.name]))[0]),
            abs(v.lng - ref.locate(Pin(v.name, *ground[v.name]))[1]))
        for v in result.venues)
    assert worst < 5e-4, worst  # ~50 m


def _placement_error_m(result, dev):
    from beer_in_this_town.app_geo import Camera, Scale
    from beer_in_this_town.app_map import Pin

    ground = {n: (x, y) for n, x, y in dev.pins}
    ref = Camera(centre=(32.0853, 34.7818), scale=Scale(m_per_px=10.0))
    worst = 0.0
    for v in result.venues:
        lat, lng = ref.locate(Pin(v.name, *ground[v.name]))
        worst = max(worst, abs(v.lat - lat) * 111_320,
                    abs(v.lng - lng) * 111_320 * 0.847)
    return worst


def test_a_pan_that_cannot_be_measured_uses_the_measured_fling():
    """Found live on a depth-3 Tel Aviv sweep, 2026-11-14: whole cells were
    placed 1-3 km off, mostly north, before any correction. A pan whose
    screens share too few pins was assumed to have landed exactly, though
    the fling overshoots, and the error carried into every later cell.
    The pans that were measured already say how much the fling overshoots."""
    from beer_in_this_town.app_geo import Camera, Scale

    cam = Camera(centre=(32.0853, 34.7818), scale=Scale(m_per_px=10.0))
    dev = WorldDevice(fling=1.2, blind=range(4, 200, 3))
    result = sweep(dev, CELL, max_depth=2, verify_pans=True, camera=cam,
                   settle_min_s=0, settle_max_s=0)
    assert any(p["measured"] is None for p in result.pans)
    assert _placement_error_m(result, dev) < 120


def test_every_pan_is_recorded_for_the_post_mortem():
    """Nothing recorded the pans, so the Tel Aviv drift could be traced to a
    cell but not to the gesture that caused it."""
    dev = WorldDevice(fling=1.15)
    result = sweep(dev, CELL, max_depth=1, verify_pans=True,
                   settle_min_s=0, settle_max_s=0)
    assert result.pans
    first = result.pans[0]
    assert set(first) >= {"cell", "kind", "aim", "measured", "shared", "used"}
    assert first["kind"] == "pan"
