"""Sweep a city out of the app's map by recursive subdivision.

`app_map.py` reads one screen. This walks many of them.

**The rule the sweep turns on.** A search returns a result set capped at
about 58 venues, so a cell that comes back at the cap was truncated and hides
more; a cell that comes back under it is taken as complete.

**The rule is about the result set, not the screen**, and that distinction
bit on the first live run: a map left panned from earlier work showed 56 of a
58-venue result set, `is_truncated(56)` said "complete", and the sweep
declined to subdivide a cell that was in fact full. Pins are clipped to the
viewport; the cap is not. So **every cell re-searches before it harvests** --
in Untappd 4.x by pressing `Refresh search`, in 5.x by the pan itself (see
below) -- which puts the whole result set inside the viewport by construction
and makes the pin count mean what the rule assumes. Measured on the
live app: two independent Tel Aviv searches both returned exactly 58, Haifa
returned 37. That single number replaces every guess about grid spacing --
density decides how deep to divide, and no requests are spent on empty
countryside.

**Why subdivision rather than one big viewport.** Zooming one level into a
dense cell revealed eight venues the wider view never showed. No single
viewport enumerates an area, so a city is a tree, not a rectangle.

**Why a pan is enough (Untappd 5.x).** The app re-queries by itself once a
pan settles. Measured 2026-11-14: four quarter-screen pans over filtered Tel
Aviv saw 71 distinct venues, more than one result set holds, and each view's
pins were final within 4 s -- inside the settle. So a child cell is a pan and
a wait. (Untappd 4.x only re-drew on a pan, and every cell had to press
`Refresh search`; 5.x removed that button.) The category filter survives the
re-query, so it is set once per sweep, not per cell.

**Every control is found by its label.** 4.x controls were tapped at
remembered positions, and 5.x moved every one of them. A label that is not on
screen raises `WrongScreen` before anything is touched: a tap at a stale
position lands on whatever is there now.

The sweep runs unattended, so every judgement a person made during the
research is a check here: that the app in front of us is the app, that the
screen is the map and not the list, that the map actually moved when asked,
and that a cell at the cap is divided rather than believed. Each of those was
a real failure first -- a harvest that captured the BlueStacks launcher and
reported its five nodes as data, and one that read a dead scroll as a
finished list. Neither raised. Both returned believable numbers.

The device is behind a two-method protocol so all of this is testable without
one, and so nothing here needs a model at runtime.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .adb_device import ScreenNotReady
from .app_categories import is_drinking_category, plan_category_taps
from .app_map import (
    Pin,
    WrongScreen,
    categories_in,
    is_truncated,
    pin_displacement,
    pins_in,
    require_map_screen,
)
from .config import STATE_DIR, city_slug

if TYPE_CHECKING:
    # `app_geo` imports this module for `Cell` and the screen geometry, so a
    # runtime import here would be a cycle. The sweep never builds a camera;
    # the caller hands one in.
    from .app_geo import Camera

log = logging.getLogger(__name__)

_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

MAP_PACKAGE = "com.untappdllc.app"

# The map area of the screen, below the search bar and above the tab bar.
# Pans are measured against this, not the whole display.
MAP_TOP = 192
MAP_BOTTOM = 1516
SCREEN_WIDTH = 900

# The selected-venue card overlays the bottom of the map, from here down to
# the tab bar. It looks like map and is not: a swipe that starts on it drags
# the card, the map stays exactly still, and `DeadPan` stops the sweep. Found
# twice live before the cause was understood -- a pan of (-225,-331) px moved
# the map by precisely (0,0) across 58 shared pins.
CARD_TOP = 1242

# Controls, by the label Untappd 5.x gives them (content-desc or text).
# Found in each dump, never tapped by position: see the module docstring.
FILTER_LABEL = "Filter"                    # map -> filter panel
CATEGORY_LABEL = "Filter by Category"      # filter panel -> category list
DESELECT_ALL_LABEL = "Deselect All"        # category list, anything ticked
SELECT_ALL_LABEL = "Select All"            # the same toggle, nothing ticked
APPLY_LABEL = "Apply"                      # category list -> filter panel
SHOW_RESULTS_LABEL = "Show Results"        # filter panel -> map, re-queries
LOCATION_SHEET_LABEL = "Choose Location"   # the city-search sheet
CLEAR_LABEL = "Clear"                      # empties the sheet's text box

# How many tap-and-rescan passes the category panel gets before we accept it
# as set. It scrolls, so one pass only reaches the visible rows.
MAX_CATEGORY_PASSES = 10

# Discover -> the `Map` button (4.x called it "View Map"). Located by its
# label in the dump, never tapped by a remembered position: recovery runs
# precisely when the screen is not what was expected, which is the one moment
# a remembered position is least safe.
VIEW_MAP_LABEL = "Map"

# How many looks a relaunch gets before the screen is declared wrong. At
# half a settle apart that is roughly 20-30 s, well past a measured cold start.
RELAUNCH_POLLS = 8

# The place-name button at the top left of the map opens the city search.
# Its label *is* the current place ("Haifa, Israel"), so it cannot be found
# by label; it is the wide button in the header's top-left corner. The
# filter panel's `Close` sits in the same corner but is 66 px wide.
LOCATION_BUTTON_MAX_X1 = 60
LOCATION_BUTTON_MAX_Y2 = 120
LOCATION_BUTTON_MIN_WIDTH = 150

# Slow enough that the fling is small; the caller measures what actually
# happened rather than trusting it. A 400 ms swipe overshot by 27%.
PAN_MS = 1200

# A pan that moves the map by fewer pixels than this did not really happen.
MIN_PAN_PX = 20.0

# How many pins must survive a pan before its displacement is worth trusting.
#
# Below this the measurement says nothing: a large pan legitimately leaves
# almost no overlap, and the handful that remain are usually clustered
# markers, which the map re-lays-out. Firing `DeadPan` on three shared pins
# that moved (10,9) px was a false accusation -- the map had moved so far
# that only three pins were left to compare.
MIN_SHARED_FOR_PAN_CHECK = 6

# How many pans may stall before the sweep gives up.
#
# One dead pan is a cell worth skipping; a run of them means the gesture is
# not working at all and every later cell would re-harvest the same
# rectangle. Same shape as the circuit breaker in `guardrails.py`: tolerate
# the occasional failure, stop when it looks systematic. Non-consecutive
# stalls do not accumulate, because a single awkward viewport is not a
# broken sweep.
MAX_CONSECUTIVE_DEAD_PANS = 3

# How much neighbouring cells overlap, as a fraction of a cell's width.
#
# Exact tiling loses venues at the seams, for three measured reasons: the
# fling is not exact (0.95 to 1.27 depending on swipe duration, and
# `pin_displacement` measures the error without correcting it); a marker
# straddling the viewport edge may not render at all; and the map nudges
# overlapping markers apart by up to ~100 px in dense clusters.
#
# 15% of a quarter-viewport is ~34 px horizontally, about 350 m on the
# ground at the city zoom -- comfortably more than any of those three. The
# cost is that cells re-harvest their margins, which dedup absorbs: a
# duplicate venue is free, a missed one is invisible.
CELL_OVERLAP = 0.15

# How long to let the app settle after a gesture before believing the screen.
# A pan re-queries the network (Untappd 5.x does it by itself; measured final
# within 4 s), and a dump taken too early catches a half-drawn map -- which
# reads as a thinner city, not as an error.
# Jittered, because a fixed interval is a tell and this drives a real
# account; same reasoning as `http_client`'s min/max delay.
SETTLE_MIN_S = 5.0
SETTLE_MAX_S = 8.0

# The category panel is local: opening it, ticking a row and scrolling it
# involve no network, so they do not need a search's patience. Measured, the
# filter pass at full settles cost ~90 s per cell and was almost entirely
# waiting. `SHOW RESULTS` is the exception -- it issues a query -- and keeps
# the full settle.
PANEL_SETTLE_MIN_S = 0.4
PANEL_SETTLE_MAX_S = 0.9

# How far a pan may land from where it was aimed before it is corrected.
# The fling overshoots by up to 27%, and `pin_displacement` was measuring
# that error without anyone acting on it. Below this, correcting costs more
# than the drift; the cells overlap by ~34 px, which absorbs it.
PAN_TOLERANCE_PX = 25.0


@runtime_checkable
class Device(Protocol):
    """Whatever can drive the phone. `adb` in production, a fake in tests."""

    def focused_package(self) -> str: ...
    def dump(self) -> str: ...
    def tap(self, x: int, y: int) -> None: ...
    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int) -> None: ...
    def type_text(self, text: str) -> None: ...
    def press_enter(self) -> None: ...
    def launch(self, package: str) -> None: ...


class DeadPan(RuntimeError):
    """The map did not move when we panned it.

    Distinct from "this cell is empty": a gesture that is not landing makes
    every subsequent cell re-harvest the same place while the sweep reports
    progress, which is a short corpus that looks complete.
    """


@dataclass(frozen=True)
class Cell:
    """A geographic rectangle to sweep, in degrees."""

    left: float
    right: float
    bottom: float
    top: float

    def quarters(self, overlap: float = CELL_OVERLAP) -> list[Cell]:
        """Four children that overlap rather than tile.

        A seam is where venues go missing: the pan that lands a child is not
        pixel-exact, a marker on the boundary may not render, and dense
        markers are drawn displaced. Overlapping costs duplicates, which
        dedup absorbs for nothing.
        """
        mx = (self.left + self.right) / 2
        my = (self.bottom + self.top) / 2
        px = (self.right - self.left) / 2 * overlap
        py = (self.top - self.bottom) / 2 * overlap
        return [
            Cell(self.left, mx + px, my - py, self.top),
            Cell(mx - px, self.right, my - py, self.top),
            Cell(self.left, mx + px, self.bottom, my + py),
            Cell(mx - px, self.right, self.bottom, my + py),
        ]


@dataclass(frozen=True)
class Venue:
    """A venue as the map knows it, before any enrichment.

    `x`/`y` are screen pixels **in the dump that found it** and mean nothing
    outside that one viewport -- they are kept for debugging a single cell,
    not for locating anything. `lat`/`lng` are where it is, and are `None`
    when the sweep ran without a `Camera`: unknown, never guessed.
    """

    name: str
    x: int
    y: int
    lat: float | None = None
    lng: float | None = None
    # Which cell (visit order) found it. Every pin in one cell shares one
    # camera position, so a cell is the unit placement can be corrected in.
    cell: int | None = None


@dataclass
class SweepResult:
    """What was found, and what the sweep itself doubts about it.

    `truncated_cells` and `hit_depth_limit` are the honesty of the thing: a
    caller must be able to tell a complete sweep from one that stopped short,
    because the venue list looks identical either way.
    """

    venues: list[Venue] = field(default_factory=list)
    cells_visited: int = 0
    truncated_cells: int = 0
    skipped_cells: int = 0
    hit_depth_limit: bool = False
    warnings: list[str] = field(default_factory=list)
    _consecutive_dead_pans: int = 0
    # The screen as it was after the most recent dump, wherever the map is
    # now. Every pan is measured against this, never against an older one.
    _screen: list[Pin] = field(default_factory=list)
    # Where the last finished cell left the map, relative to its own centre:
    # what its hand-back pan did not quite undo. The parent adds it to its
    # own reckoning instead of assuming the child came back exactly.
    _left_at: tuple[float, float] = (0.0, 0.0)


def _require_app(device: Device) -> None:
    focused = device.focused_package()
    if MAP_PACKAGE not in focused:
        raise WrongScreen(
            f"focused app is {focused!r}, not {MAP_PACKAGE}. "
            "Refusing to harvest a screen that is not Untappd.")


def _find_label(xml: str, label: str) -> tuple[int, int] | None:
    """The centre of the node whose text or description is exactly `label`."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        if label in ((node.get("text") or "").strip(),
                     (node.get("content-desc") or "").strip()):
            m = _BOUNDS.match(node.get("bounds") or "")
            if m:
                x1, y1, x2, y2 = (int(g) for g in m.groups())
                return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def _settle(lo: float, hi: float) -> None:
    if hi > 0:
        time.sleep(random.uniform(lo, hi))


def _tap_label(device: Device, label: str, xml: str | None = None) -> None:
    """Tap the control labelled `label` in the current dump, or refuse.

    Refusing is the point: a missing label means the screen is not the one we
    think, and a tap anyway lands on whatever is there.
    """
    target = _find_label(xml if xml is not None else device.dump(), label)
    if target is None:
        raise WrongScreen(
            f"no {label!r} control on screen. Refusing to tap blind; the app "
            "is not on the screen the sweep expected.")
    device.tap(*target)


def _bounds_of(node: ET.Element) -> tuple[int, int, int, int] | None:
    m = _BOUNDS.match(node.get("bounds") or "")
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def _location_button(xml: str) -> tuple[int, int] | None:
    """The centre of the place-name button that opens the city search."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        if not (node.get("class") or "").endswith("Button"):
            continue
        box = _bounds_of(node)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        if (x1 <= LOCATION_BUTTON_MAX_X1 and y2 <= LOCATION_BUTTON_MAX_Y2
                and x2 - x1 >= LOCATION_BUTTON_MIN_WIDTH):
            return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def _text_field(xml: str) -> tuple[int, int] | None:
    """The centre of the first text box in the dump."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        if (node.get("class") or "").endswith("EditText"):
            box = _bounds_of(node)
            if box is not None:
                return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    return None


def _clear_origin(pins: list[Pin], dx: int, dy: int) -> tuple[int, int]:
    """Somewhere to start a pan that is not on top of a marker.

    Two things absorb a swipe and leave the map still, both found live by
    `DeadPan`: a marker under the finger, and the venue card overlaying the
    bottom of the screen. Candidates are spread across the map area above the
    card, and whichever is furthest from every pin wins. The chosen point
    also has to leave room for the gesture without running off the map.
    """
    lo_x, hi_x = 80, SCREEN_WIDTH - 80
    # Stay above the venue card: it is not map, however much it looks like it.
    lo_y, hi_y = MAP_TOP + 80, CARD_TOP - 60
    candidates = [
        (x, y)
        for x in range(lo_x, hi_x + 1, 100)
        for y in range(lo_y, hi_y + 1, 120)
        if lo_x <= x + dx <= hi_x and lo_y <= y + dy <= hi_y
    ]
    if not candidates:
        return SCREEN_WIDTH // 2, (MAP_TOP + MAP_BOTTOM) // 2

    def clearance(point: tuple[int, int]) -> float:
        if not pins:
            return float("inf")
        return min((point[0] - p.x) ** 2 + (point[1] - p.y) ** 2 for p in pins)

    return max(candidates, key=clearance)


def _follow(camera: Camera | None, dx: float, dy: float) -> None:
    """Move the camera with the content. A no-op when nobody is tracking."""
    if camera is not None:
        camera.pan_px(dx, dy)


def _pan(device: Device, dx: int, dy: int, pins: list[Pin]) -> None:
    cx, cy = _clear_origin(pins, dx, dy)
    device.swipe(cx, cy, cx + dx, cy + dy, PAN_MS)


def journal_path(city: str) -> Path:
    """Where a sweep of `city` records what it has already found.

    Scoped by city, like `pinned_<list>.json` and `previous_run_<query>.json`
    before it: an unscoped journal would let a Haifa sweep resume into a Tel
    Aviv corpus.
    """
    return STATE_DIR / f"swept_{city_slug(city)}.json"


def load_journal(city: str) -> SweepResult:
    """Resume a sweep, or start one.

    A sweep is minutes of real gestures against a real account, so losing it
    to a crash on the nineteenth cell is expensive. What is *not* recorded is
    which cells were visited: the geometry is cheap to redo and a resumed
    sweep re-walking a cell costs one dump, while a wrongly-skipped cell
    costs venues silently.
    """
    path = journal_path(city)
    if not path.exists():
        return SweepResult()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("Sweep journal for %s was unreadable; starting fresh.", city)
        return SweepResult()
    return SweepResult(
        venues=[Venue(**v) for v in raw.get("venues", [])],
        cells_visited=raw.get("cells_visited", 0),
        truncated_cells=raw.get("truncated_cells", 0),
        skipped_cells=raw.get("skipped_cells", 0),
        hit_depth_limit=raw.get("hit_depth_limit", False),
        warnings=list(raw.get("warnings", [])))


def save_journal(city: str, result: SweepResult) -> None:
    """Write progress after every cell, not at the end.

    Writing once at the end would make the journal useless for the only case
    it exists to serve.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "city": city,
        "venues": [{"name": v.name, "x": v.x, "y": v.y,
                    "lat": v.lat, "lng": v.lng, "cell": v.cell}
                   for v in result.venues],
        "cells_visited": result.cells_visited,
        "truncated_cells": result.truncated_cells,
        "skipped_cells": result.skipped_cells,
        "hit_depth_limit": result.hit_depth_limit,
        "warnings": result.warnings,
    }
    journal_path(city).write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")


# How long a journal stays worth resuming or reusing. Past it, a sweep of the
# same city starts over: the map has moved on, and a journal that never
# expired carried last week's venues into this week's corpus.
JOURNAL_MAX_AGE_H = 12.0


@dataclass(frozen=True)
class FinishedSweep:
    """A sweep that walked every cell, and where its map was centred."""

    result: SweepResult
    centre: tuple[float, float]
    centre_source: str


def mark_complete(city: str, result: SweepResult,
                  centre: tuple[float, float], centre_source: str) -> None:
    """Record that every cell was walked.

    The steps after the sweep can fail on their own -- Overpass sheds load
    with a 504 as a matter of course. Measured 2026-09-22: a complete Tel Aviv
    sweep failed at placement, and the re-run walked every cell again,
    because a resumed journal re-walks cells by design. With this mark, the
    re-run places the finished sweep instead.
    """
    save_journal(city, result)
    path = journal_path(city)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(complete=True, centre=list(centre),
                   centre_source=centre_source)
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                    encoding="utf-8")


def _journal_age_h(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 3600


def load_finished(city: str,
                  max_age_h: float = JOURNAL_MAX_AGE_H) -> FinishedSweep | None:
    """A complete, recent sweep of `city`, or None."""
    path = journal_path(city)
    try:
        if _journal_age_h(path) > max_age_h:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    centre = raw.get("centre")
    if not raw.get("complete") or not centre or len(centre) != 2:
        return None
    return FinishedSweep(result=load_journal(city),
                         centre=(float(centre[0]), float(centre[1])),
                         centre_source=str(raw.get("centre_source", "")))


def discard_journal(city: str, *, only_if_older_than_h: float | None = None
                    ) -> bool:
    """Forget a sweep of `city`, always or only once it is stale."""
    path = journal_path(city)
    try:
        if (only_if_older_than_h is not None
                and _journal_age_h(path) <= only_if_older_than_h):
            return False
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def ensure_map_screen(device: Device, relaunch: bool = True,
                      settle_min_s: float = SETTLE_MIN_S,
                      settle_max_s: float = SETTLE_MAX_S) -> None:
    """Refuse to act until the map is actually in front of us.

    `require_map_screen` protects the *harvest*; this protects the
    *navigation*, which is a separate hole. A measurement run once failed
    only after its search taps had already landed, because the app was on a
    venue page from earlier work -- so a city name was typed into whatever
    happened to be focused. Unattended, that is how a sweep searches nothing
    and reports a small city.

    Recovering by pressing Back is what emptied the app to the BlueStacks
    launcher twice during the research, so this relaunches instead.
    """
    try:
        _require_app(device)
        require_map_screen(device.dump())
        return
    except WrongScreen:
        if not relaunch:
            raise

    log.warning("Not on the map; relaunching the app to get there.")
    device.launch(MAP_PACKAGE)
    _settle(settle_min_s, settle_max_s)

    # A force-stopped app cold-starts, and that takes longer than a settle:
    # the first dump after relaunch was once a half-drawn splash. So poll,
    # boundedly, for either the map itself or the way to it.
    target = None
    not_ready: ScreenNotReady | None = None
    for _ in range(RELAUNCH_POLLS):
        # A cold-starting app can have no window at all yet, and uiautomator
        # says so instead of dumping (found live on Untappd 5.0.3). Here, and
        # only here, that is expected: keep polling, boundedly.
        try:
            xml = device.dump()
        except ScreenNotReady as exc:
            not_ready = exc
            _settle(settle_min_s / 2, settle_max_s / 2)
            continue
        not_ready = None
        try:
            require_map_screen(xml)
            return
        except WrongScreen:
            pass
        target = _find_label(xml, VIEW_MAP_LABEL)
        if target is not None:
            break
        _settle(settle_min_s / 2, settle_max_s / 2)
    if target is None and not_ready is not None:
        raise not_ready
    if target is None:
        raise WrongScreen(
            f"relaunched, but there is no {VIEW_MAP_LABEL!r} button on screen "
            "to reach the map from. Refusing to tap blind; open Discover -> "
            "Map by hand and re-run.")
    device.tap(*target)
    _settle(settle_min_s, settle_max_s)

    _require_app(device)
    require_map_screen(device.dump())


def search_city(device: Device, query: str,
                settle_min_s: float = SETTLE_MIN_S,
                settle_max_s: float = SETTLE_MAX_S) -> None:
    """Move the viewport by name, from a screen known to be the map.

    Untappd 5.x: tap the place name at the top left, which opens a "Choose
    Location" sheet; clear any text left in its box; type; Enter. Enter is
    what runs the search -- the sheet offers no suggestions to pick from.

    The sheet is drawn *over* the map and the map's nodes stay in the dump,
    so `require_map_screen` passes while it is open. That is why the sheet is
    checked for by name both before typing and after the search.
    """
    ensure_map_screen(device, settle_min_s=settle_min_s,
                      settle_max_s=settle_max_s)
    button = _location_button(device.dump())
    if button is None:
        raise WrongScreen(
            "the map shows no place-name button to search from. Refusing to "
            "tap blind.")
    device.tap(*button)
    _settle(settle_min_s / 2, settle_max_s / 2)

    xml = device.dump()
    if _find_label(xml, LOCATION_SHEET_LABEL) is None:
        raise WrongScreen(
            f"tapped the place name but no {LOCATION_SHEET_LABEL!r} sheet "
            "opened. Refusing to type into whatever has focus.")
    if _find_label(xml, CLEAR_LABEL) is not None:
        _tap_label(device, CLEAR_LABEL, xml)
        _settle(settle_min_s / 2, settle_max_s / 2)
        xml = device.dump()
    field = _text_field(xml)
    if field is None:
        raise WrongScreen(
            f"the {LOCATION_SHEET_LABEL!r} sheet has no text box to type into.")
    device.tap(*field)
    _settle(settle_min_s / 2, settle_max_s / 2)
    device.type_text(query)
    _settle(settle_min_s / 2, settle_max_s / 2)
    device.press_enter()
    # A search is a network round trip, so it gets longer than a gesture.
    _settle(settle_min_s * 2, settle_max_s * 2)

    xml = device.dump()
    if _find_label(xml, LOCATION_SHEET_LABEL) is not None:
        raise WrongScreen(
            f"searched for {query!r} but the {LOCATION_SHEET_LABEL!r} sheet is "
            "still open: the app did not place that name. Harvesting now would "
            "read the previous city's map as this one.")
    require_map_screen(xml)


def apply_drinking_filter(device: Device,
                          settle_min_s: float = 1.0,
                          settle_max_s: float = 2.0) -> list[str]:
    """Set the category filter to drinking venues only.

    A result set holds about 60 venues and Untappd's index is not a drinking
    index -- a Tel Aviv search returns parks, markets, hostels and hotels
    beside its bars. Every slot spent on one is a bar that did not fit, and it
    cannot be corrected afterwards because pins carry no category.

    Untappd 5.x: map -> `Filter` -> `Filter by Category` -> `Deselect All`,
    tick the drinking rows, `Apply` -> `Show Results`, which re-queries and
    returns to the map. The list scrolls, so this ticks what is visible,
    re-reads, and moves down until the rows stop changing. Returns the
    categories it kept, so a caller can record what the corpus was filtered
    to.
    """
    panel_lo = min(PANEL_SETTLE_MIN_S, settle_min_s)
    panel_hi = min(PANEL_SETTLE_MAX_S, settle_max_s)

    _tap_label(device, FILTER_LABEL)
    _settle(panel_lo, panel_hi)
    _tap_label(device, CATEGORY_LABEL)
    _settle(panel_lo, panel_hi)
    # Every category normally arrives ticked; one tap turns them all off, and
    # the pass below only has to turn the drinking ones on. The button is a
    # toggle -- it reads `Select All` once nothing is ticked -- so it is
    # pressed only while it says `Deselect All`. A list left cleared by an
    # earlier run needs no press, and pressing `Select All` would tick
    # everything.
    xml = device.dump()
    if _find_label(xml, DESELECT_ALL_LABEL) is not None:
        _tap_label(device, DESELECT_ALL_LABEL, xml)
        _settle(panel_lo, panel_hi)
    elif _find_label(xml, SELECT_ALL_LABEL) is None:
        raise WrongScreen(
            f"tapped {CATEGORY_LABEL!r} but the category list did not open "
            f"(no {DESELECT_ALL_LABEL!r} or {SELECT_ALL_LABEL!r}).")

    seen: set[str] = set()
    for _ in range(MAX_CATEGORY_PASSES):
        xml = device.dump()
        cats = categories_in(xml)
        taps = plan_category_taps(cats, xml)
        for tap in taps:
            device.tap(tap.x, tap.y)
            _settle(panel_lo, panel_hi)

        fresh = set(cats) - seen
        seen.update(cats)
        if not taps and not fresh:
            break
        # Reach the rows below the fold.
        device.swipe(450, 1200, 450, 700, PAN_MS // 2)
        _settle(panel_lo, panel_hi)

    _tap_label(device, APPLY_LABEL)
    _settle(panel_lo, panel_hi)
    # This one issues a query, so it gets a search's patience.
    _tap_label(device, SHOW_RESULTS_LABEL)
    _settle(settle_min_s, settle_max_s)

    return sorted(n for n in seen if is_drinking_category(n))


def _harvest_screen(device: Device) -> list[Pin]:
    xml = device.dump()
    require_map_screen(xml)
    return pins_in(xml)


def _measure(before: list[Pin], after: list[Pin]
             ) -> tuple[float, float] | None:
    """How far the content moved between two screens, if enough pins say so."""
    moved_x, moved_y, shared = pin_displacement(before, after)
    if moved_x is None or moved_y is None or shared < MIN_SHARED_FOR_PAN_CHECK:
        return None
    return moved_x, moved_y


def _move_to(device: Device, here: tuple[float, float],
             target: tuple[float, float], result: SweepResult,
             camera: Camera | None, verify: bool,
             settle_min_s: float, settle_max_s: float,
             returning: bool = False) -> tuple[tuple[float, float], bool]:
    """Pan from `here` to `target`, both content offsets from a cell centre.

    Returns where the map really is afterwards, and whether it got there.

    Found live on Untappd 5.x: every pan used to be measured against the
    *parent's* screen, captured once, although each swipe moves the map from
    wherever the previous child left it. The second child then read as a
    whole step off target, the third as a dead pan (skipped, though the map
    had moved), and the camera added every earlier child's move again, which
    put one Tel Aviv cell 5.8 km off. So each pan here is measured against
    the screen just before it, and a correction is measured too.
    """
    dx, dy = round(target[0] - here[0]), round(target[1] - here[1])
    if abs(dx) <= PAN_TOLERANCE_PX and abs(dy) <= PAN_TOLERANCE_PX:
        return here, True

    before = result._screen
    _pan(device, dx, dy, before)
    _settle(settle_min_s, settle_max_s)
    if not verify:
        _follow(camera, dx, dy)
        result._screen = []
        return (here[0] + dx, here[1] + dy), True

    after = _harvest_screen(device)
    result._screen = after
    moved = _measure(before, after)
    # Believe the measurement when there is enough overlap to make one;
    # otherwise the request is the best estimate there is.
    mx, my = moved if moved is not None else (dx, dy)
    _follow(camera, mx, my)
    here = (here[0] + mx, here[1] + my)

    # Low overlap is evidence the map moved, not that it stalled, so the
    # guard only accuses when it has enough pins to be sure.
    if moved is not None and abs(mx) < MIN_PAN_PX and abs(my) < MIN_PAN_PX:
        result._consecutive_dead_pans += 1
        what = ("the map is not back at its parent's centre" if returning
                else "skipping this cell")
        msg = (f"pan of ({dx},{dy}) px moved the map ({mx:.0f},{my:.0f}); "
               f"{what} ({result._consecutive_dead_pans} in a row).")
        log.warning(msg)
        result.warnings.append(msg)
        if not returning:
            result.skipped_cells += 1
        if result._consecutive_dead_pans >= MAX_CONSECUTIVE_DEAD_PANS:
            raise DeadPan(
                f"{result._consecutive_dead_pans} pans in a row failed "
                "to move the map. The gesture is not reaching it, and "
                "every later cell would re-harvest the same rectangle.")
        return here, False
    result._consecutive_dead_pans = 0

    # Close the loop. The fling overshoots by up to 27%; left alone, that
    # error accumulates and shifts every later cell off its target.
    err_x, err_y = target[0] - here[0], target[1] - here[1]
    if moved is not None and (abs(err_x) > PAN_TOLERANCE_PX
                              or abs(err_y) > PAN_TOLERANCE_PX):
        log.info("Pan landed (%.0f,%.0f) px off target; correcting.",
                 err_x, err_y)
        cx, cy = round(err_x), round(err_y)
        _pan(device, cx, cy, after)
        _settle(settle_min_s, settle_max_s)
        again = _harvest_screen(device)
        result._screen = again
        fixed = _measure(after, again)
        fx, fy = fixed if fixed is not None else (cx, cy)
        _follow(camera, fx, fy)
        here = (here[0] + fx, here[1] + fy)
    return here, True


def sweep(device: Device, cell: Cell, max_depth: int = 3,
          min_depth: int = 1,
          verify_pans: bool = False, city: str | None = None,
          filter_drinking: bool = True,
          camera: Camera | None = None,
          settle_min_s: float = SETTLE_MIN_S,
          settle_max_s: float = SETTLE_MAX_S,
          _depth: int = 0,
          _result: SweepResult | None = None) -> SweepResult:
    """Harvest `cell`, subdividing wherever the result set was truncated.

    The defaults are the measured ones (docs/HARVESTING.md): one forced
    split, never deeper than 3, and the drinking-category filter on. A caller
    that wants the raw, unfiltered map has to say so.

    `max_depth` bounds the recursion. A dense centre can stay at the cap
    however far it is divided, and the sweep must stop and *say* it stopped
    rather than loop -- `hit_depth_limit` is that admission.

    `verify_pans` measures that the map actually moved between cells. It
    costs an extra dump per pan, which is why it is opt-in, and it is the
    difference between a sweep that covers a city and one that harvests the
    same rectangle repeatedly.

    `camera` gives every venue a coordinate, converted while the viewport
    that found it is still the live one, and is moved with every pan. With
    `verify_pans` it follows the *measured* displacement; without, it follows
    the requested one, which the fling makes 5% wrong either way at
    `PAN_MS`. Without a camera, venues carry no coordinates at all.

    `settle_min_s`/`settle_max_s` are how long to wait after a gesture before
    believing the screen. Set them to 0 in tests; do not lower them against a
    real device, where a dump taken mid-redraw reads as a thinner city rather
    than as an error.
    """
    if _result is not None:
        result = _result
    elif city:
        result = load_journal(city)
        if result.venues:
            log.info("Resuming sweep of %s with %d venue(s) already found.",
                     city, len(result.venues))
    else:
        result = SweepResult()

    if _depth == 0:
        _require_app(device)

    # The root viewport has already been queried by the city search, and a
    # child's by the pan that reached it (Untappd 5.x re-queries on its own).
    # The filter is set once, at the root: `Show Results` re-queries the root
    # viewport, and the filter survives every later automatic re-query.
    if filter_drinking and _depth == 0:
        apply_drinking_filter(device, settle_min_s, settle_max_s)

    pins = _harvest_screen(device)
    result._screen = pins
    result._left_at = (0.0, 0.0)
    result.cells_visited += 1

    known = {v.name for v in result.venues}
    for pin in pins:
        if pin.name not in known:
            known.add(pin.name)
            lat, lng = camera.locate(pin) if camera else (None, None)
            result.venues.append(
                Venue(name=pin.name, x=pin.x, y=pin.y, lat=lat, lng=lng,
                      cell=result.cells_visited))

    if city:
        save_journal(city, result)

    # `not truncated` does NOT mean `complete`, and Singapore proved it: a
    # search for a city-state lands the map at country zoom, where the
    # viewport is enormous and the result set sparse. It returned 13 venues,
    # was under the cap, declared itself finished -- and matched **5 of the
    # 100** venues in the known top-100 corpus. Few pins over a huge area
    # reads exactly like few pins over a small one.
    #
    # So the cap cannot be the only reason to subdivide. `min_depth` forces
    # splitting regardless of it, which is the crude form of bounding a cell
    # by ground size. The proper form is `min_cell_km`, and it needs
    # metres-per-pixel for this city's zoom -- which is not yet measurable,
    # because the app chooses the zoom and the scale changes with it.
    if not is_truncated(len(pins)) and _depth >= min_depth:
        log.info("Cell complete: %d venue(s).", len(pins))
        return result

    if not is_truncated(len(pins)):
        log.info("Cell under the cap at depth %d but min_depth is %d; "
                 "splitting anyway -- a sparse wide viewport looks identical "
                 "to a complete small one.", _depth, min_depth)

    if is_truncated(len(pins)):
        result.truncated_cells += 1

    if _depth >= max_depth:
        result.hit_depth_limit = True
        msg = (f"Stopped at depth {max_depth} with a cell still at the "
               f"result-set cap ({len(pins)} venues). This area holds more "
               f"than was collected; raise max_depth to go further.")
        log.warning(msg)
        result.warnings.append(msg)
        return result

    # A quarter of the map area, which is how far the viewport must move to
    # land on each child cell.
    # Short of a true quarter, so neighbouring viewports overlap. See
    # CELL_OVERLAP for why exact tiling loses venues at the seams.
    step_x = int(SCREEN_WIDTH // 4 * (1 - CELL_OVERLAP))
    step_y = int((MAP_BOTTOM - MAP_TOP) // 4 * (1 - CELL_OVERLAP))

    # Where the map is now, as a content offset from this cell's centre.
    # A swipe is relative to wherever the map happens to be, so each child is
    # aimed from `here`, and `here` follows what each pan actually did.
    here = (0.0, 0.0)
    for target in ((-step_x, -step_y), (step_x, -step_y),
                   (-step_x, step_y), (step_x, step_y)):
        here, arrived = _move_to(device, here, target, result, camera,
                                 verify_pans, settle_min_s, settle_max_s)
        if not arrived:
            continue
        # The pan's own re-query has landed during the settle, so the child
        # harvests what is drawn. It hands the map back at its own centre,
        # which is `here`.
        sweep(device, cell, max_depth=max_depth, min_depth=min_depth,
              verify_pans=verify_pans,
              city=city, filter_drinking=filter_drinking, camera=camera,
              settle_min_s=settle_min_s, settle_max_s=settle_max_s,
              _depth=_depth + 1, _result=result)
        here = (here[0] + result._left_at[0], here[1] + result._left_at[1])

    # Hand the map back where this cell found it, so the parent can aim at
    # its next child from a known place.
    result._left_at, _ = _move_to(device, here, (0.0, 0.0), result, camera,
                                  verify_pans, settle_min_s, settle_max_s,
                                  returning=True)
    return result
