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
`Refresh search` queries the current viewport, which puts the whole result
set inside it by construction and makes the pin count mean what the rule
assumes. Measured on the
live app: two independent Tel Aviv searches both returned exactly 58, Haifa
returned 37. That single number replaces every guess about grid spacing --
density decides how deep to divide, and no requests are spent on empty
countryside.

**Why subdivision rather than one big viewport.** Zooming one level into a
dense cell revealed eight venues the wider view never showed. No single
viewport enumerates an area, so a city is a tree, not a rectangle.

**Why `Refresh search` per cell.** Panning re-draws; it does not re-query. A
child cell that is panned into but not re-searched shows a slice of its
parent's result set and finds nothing new -- which looks exactly like a
saturated cell, and would end the recursion early with a confident wrong
answer.

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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

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
from .config import STATE_DIR, scope_slug

log = logging.getLogger(__name__)

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

# Controls on the map and in the filter panel. Tapped by position because
# their bounds are stable within a session; the descriptions record what each
# one is, so a layout change is a lookup rather than a hunt.
REFRESH_BUTTON = (855, 315)      # content-desc "Refresh search"
FILTERS_BUTTON = (774, 81)        # content-desc "filters"
CATEGORY_ROW = (450, 185)         # "Filter by Category"
APPLY_BUTTON = (845, 78)          # "APPLY"
SHOW_RESULTS_BUTTON = (450, 1556) # "SHOW RESULTS"

# How many tap-and-rescan passes the category panel gets before we accept it
# as set. It scrolls, so one pass only reaches the visible rows.
MAX_CATEGORY_PASSES = 10

# Discover -> "View Map". Tapped by position; the row is stable in-session.
VIEW_MAP_ROW = (450, 388)
SEARCH_BOX = (440, 82)
CLEAR_SEARCH = (723, 82)

# `Refresh search`, found by content-desc in the live app. Kept as a constant
# so a layout change is one edit rather than a hunt through the sweep.
REFRESH_DESC = "Refresh search"

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

# How long to let the app settle after a gesture before believing the screen.
# A `Refresh search` re-queries the network, and a dump taken too early
# catches a half-drawn map -- which reads as a thinner city, not as an error.
# Jittered, because a fixed interval is a tell and this drives a real
# account; same reasoning as `http_client`'s min/max delay.
SETTLE_MIN_S = 5.0
SETTLE_MAX_S = 8.0


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

    def quarters(self) -> list[Cell]:
        mx = (self.left + self.right) / 2
        my = (self.bottom + self.top) / 2
        return [
            Cell(self.left, mx, my, self.top),
            Cell(mx, self.right, my, self.top),
            Cell(self.left, mx, self.bottom, my),
            Cell(mx, self.right, self.bottom, my),
        ]


@dataclass(frozen=True)
class Venue:
    """A venue as the map knows it, before any enrichment."""

    name: str
    x: int
    y: int


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


def _require_app(device: Device) -> None:
    focused = device.focused_package()
    if MAP_PACKAGE not in focused:
        raise WrongScreen(
            f"focused app is {focused!r}, not {MAP_PACKAGE}. "
            "Refusing to harvest a screen that is not Untappd.")


def _settle(lo: float, hi: float) -> None:
    if hi > 0:
        time.sleep(random.uniform(lo, hi))


def _refresh(device: Device) -> None:
    """Re-query for the current viewport.

    Tapped by position because the button's bounds are stable within a
    session; `REFRESH_DESC` records what it is so a future reader can find it
    by description instead if the layout moves.
    """
    device.tap(*REFRESH_BUTTON)


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


def _pan(device: Device, dx: int, dy: int, pins: list[Pin]) -> None:
    cx, cy = _clear_origin(pins, dx, dy)
    device.swipe(cx, cy, cx + dx, cy + dy, PAN_MS)


def journal_path(city: str) -> Path:
    """Where a sweep of `city` records what it has already found.

    Scoped by city, like `pinned_<list>.json` and `previous_run_<query>.json`
    before it: an unscoped journal would let a Haifa sweep resume into a Tel
    Aviv corpus.
    """
    return STATE_DIR / f"swept_{scope_slug(city)}.json"


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
        "venues": [{"name": v.name, "x": v.x, "y": v.y} for v in result.venues],
        "cells_visited": result.cells_visited,
        "truncated_cells": result.truncated_cells,
        "skipped_cells": result.skipped_cells,
        "hit_depth_limit": result.hit_depth_limit,
        "warnings": result.warnings,
    }
    journal_path(city).write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")


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
    device.tap(*VIEW_MAP_ROW)
    _settle(settle_min_s, settle_max_s)

    _require_app(device)
    require_map_screen(device.dump())


def search_city(device: Device, query: str,
                settle_min_s: float = SETTLE_MIN_S,
                settle_max_s: float = SETTLE_MAX_S) -> None:
    """Move the viewport by name, from a screen known to be the map."""
    ensure_map_screen(device, settle_min_s=settle_min_s,
                      settle_max_s=settle_max_s)
    device.tap(*CLEAR_SEARCH)
    _settle(settle_min_s / 2, settle_max_s / 2)
    device.tap(*SEARCH_BOX)
    _settle(settle_min_s / 2, settle_max_s / 2)
    device.type_text(query)
    _settle(settle_min_s / 2, settle_max_s / 2)
    device.press_enter()
    # A search is a network round trip, so it gets longer than a gesture.
    _settle(settle_min_s * 2, settle_max_s * 2)


def apply_drinking_filter(device: Device,
                          settle_min_s: float = 1.0,
                          settle_max_s: float = 2.0) -> list[str]:
    """Set the category filter to drinking venues only, before searching.

    A result set holds about 60 venues and Untappd's index is not a drinking
    index -- only 6 of the 17 categories a Tel Aviv search returns are places
    you can drink. Every slot spent on a park, a hotel or a supermarket is a
    bar that did not fit, and it cannot be corrected afterwards because pins
    carry no category.

    The panel scrolls, so this taps what is visible, re-reads, and moves
    down until the rows stop changing. Returns the categories it ended up
    keeping, so a caller can record what the corpus was filtered to.
    """
    device.tap(*FILTERS_BUTTON)
    _settle(settle_min_s, settle_max_s)
    device.tap(*CATEGORY_ROW)
    _settle(settle_min_s, settle_max_s)

    kept: set[str] = set()
    seen: set[str] = set()
    for _ in range(MAX_CATEGORY_PASSES):
        xml = device.dump()
        cats = categories_in(xml)
        kept.update(n for n, c in cats.items() if c.checked)
        taps = plan_category_taps(cats, xml)
        for tap in taps:
            device.tap(tap.x, tap.y)
            _settle(settle_min_s / 2, settle_max_s / 2)

        fresh = set(cats) - seen
        seen.update(cats)
        if not taps and not fresh:
            break
        # Reach the rows below the fold.
        device.swipe(450, 1200, 450, 700, PAN_MS // 2)
        _settle(settle_min_s, settle_max_s)

    device.tap(*APPLY_BUTTON)
    _settle(settle_min_s, settle_max_s)
    device.tap(*SHOW_RESULTS_BUTTON)
    _settle(settle_min_s, settle_max_s)

    return sorted(n for n in seen if is_drinking_category(n))


def _harvest_screen(device: Device) -> list[Pin]:
    xml = device.dump()
    require_map_screen(xml)
    return pins_in(xml)


def sweep(device: Device, cell: Cell, max_depth: int = 3,
          verify_pans: bool = False, city: str | None = None,
          filter_drinking: bool = False,
          settle_min_s: float = SETTLE_MIN_S,
          settle_max_s: float = SETTLE_MAX_S,
          _depth: int = 0,
          _result: SweepResult | None = None) -> SweepResult:
    """Harvest `cell`, subdividing wherever the result set was truncated.

    `max_depth` bounds the recursion. A dense centre can stay at the cap
    however far it is divided, and the sweep must stop and *say* it stopped
    rather than loop -- `hit_depth_limit` is that admission.

    `verify_pans` measures that the map actually moved between cells. It
    costs an extra dump per pan, which is why it is opt-in, and it is the
    difference between a sweep that covers a city and one that harvests the
    same rectangle repeatedly.

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

    # Re-query for this exact viewport before believing what is drawn. A
    # stale result set from a previous search is clipped to the screen, and a
    # clipped count reads as a smaller city rather than as a truncation.
    #
    # `Refresh search` **clears the category filter** -- measured: 9 categories
    # checked before, all 17 after. So a filtered sweep runs the filter pass
    # *instead of* the refresh, because `SHOW RESULTS` is itself a search of
    # the current viewport. Refreshing and then filtering would work too and
    # costs an extra query; refreshing *after* filtering silently undoes it,
    # which is what made a whole filtered sweep come back unfiltered.
    if filter_drinking:
        apply_drinking_filter(device, settle_min_s, settle_max_s)
    else:
        _refresh(device)
        _settle(settle_min_s, settle_max_s)

    pins = _harvest_screen(device)
    result.cells_visited += 1

    known = {v.name for v in result.venues}
    for pin in pins:
        if pin.name not in known:
            known.add(pin.name)
            result.venues.append(Venue(name=pin.name, x=pin.x, y=pin.y))

    if city:
        save_journal(city, result)

    if not is_truncated(len(pins)):
        log.info("Cell complete: %d venue(s).", len(pins))
        return result

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
    step_x = SCREEN_WIDTH // 4
    step_y = (MAP_BOTTOM - MAP_TOP) // 4

    for dx, dy in ((-step_x, -step_y), (step_x, -step_y),
                   (-step_x, step_y), (step_x, step_y)):
        before = pins if verify_pans else None
        _pan(device, dx, dy, pins)
        _settle(settle_min_s, settle_max_s)

        if verify_pans:
            after = _harvest_screen(device)
            moved_x, moved_y, shared = pin_displacement(before or [], after)
            # Low overlap is evidence the map moved, not that it stalled,
            # so the guard only accuses when it has enough pins to be sure.
            stalled = (shared >= MIN_SHARED_FOR_PAN_CHECK
                       and moved_x is not None and moved_y is not None
                       and abs(moved_x) < MIN_PAN_PX
                       and abs(moved_y) < MIN_PAN_PX)
            if stalled:
                result._consecutive_dead_pans += 1
                msg = (f"pan of ({dx},{dy}) px moved the map "
                       f"({moved_x:.0f},{moved_y:.0f}) across {shared} shared "
                       f"pin(s); skipping this cell "
                       f"({result._consecutive_dead_pans} in a row).")
                log.warning(msg)
                result.warnings.append(msg)
                result.skipped_cells += 1
                if result._consecutive_dead_pans >= MAX_CONSECUTIVE_DEAD_PANS:
                    raise DeadPan(
                        f"{result._consecutive_dead_pans} pans in a row failed "
                        "to move the map. The gesture is not reaching it, and "
                        "every later cell would re-harvest the same rectangle.")
                continue
            result._consecutive_dead_pans = 0

        # The child re-searches on entry, so nothing is needed here.
        sweep(device, cell, max_depth=max_depth, verify_pans=verify_pans,
              city=city, filter_drinking=filter_drinking,
              settle_min_s=settle_min_s, settle_max_s=settle_max_s,
              _depth=_depth + 1, _result=result)

    return result
