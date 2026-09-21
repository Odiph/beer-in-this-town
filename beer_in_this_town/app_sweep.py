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

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .app_map import (
    Pin,
    WrongScreen,
    is_truncated,
    pin_displacement,
    pins_in,
    require_map_screen,
)

log = logging.getLogger(__name__)

MAP_PACKAGE = "com.untappdllc.app"

# The map area of the screen, below the search bar and above the tab bar.
# Pans are measured against this, not the whole display.
MAP_TOP = 192
MAP_BOTTOM = 1516
SCREEN_WIDTH = 900

# `Refresh search`, found by content-desc in the live app. Kept as a constant
# so a layout change is one edit rather than a hunt through the sweep.
REFRESH_DESC = "Refresh search"

# Slow enough that the fling is small; the caller measures what actually
# happened rather than trusting it. A 400 ms swipe overshot by 27%.
PAN_MS = 1200

# A pan that moves the map by fewer pixels than this did not really happen.
MIN_PAN_PX = 20.0

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
    hit_depth_limit: bool = False
    warnings: list[str] = field(default_factory=list)


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
    device.tap(855, 315)


def _clear_origin(pins: list[Pin], dx: int, dy: int) -> tuple[int, int]:
    """Somewhere to start a pan that is not on top of a marker.

    Dragging a pin does not move the map -- found live, when a pan of
    (225,331) px moved the map by (-2,0) and `DeadPan` stopped the sweep. The
    candidates are spread across the map area, and whichever is furthest from
    every pin wins. The chosen point also has to leave room for the gesture
    without running off the map.
    """
    lo_x, hi_x = 80, SCREEN_WIDTH - 80
    lo_y, hi_y = MAP_TOP + 80, MAP_BOTTOM - 80
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


def _harvest_screen(device: Device) -> list[Pin]:
    xml = device.dump()
    require_map_screen(xml)
    return pins_in(xml)


def sweep(device: Device, cell: Cell, max_depth: int = 3,
          verify_pans: bool = False,
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
    result = _result if _result is not None else SweepResult()

    if _depth == 0:
        _require_app(device)

    # Re-query for this exact viewport before believing what is drawn. A
    # stale result set from a previous search is clipped to the screen, and a
    # clipped count reads as a smaller city rather than as a truncation.
    _refresh(device)
    _settle(settle_min_s, settle_max_s)

    pins = _harvest_screen(device)
    result.cells_visited += 1

    known = {v.name for v in result.venues}
    for pin in pins:
        if pin.name not in known:
            known.add(pin.name)
            result.venues.append(Venue(name=pin.name, x=pin.x, y=pin.y))

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
            stalled = (shared and moved_x is not None and moved_y is not None
                       and abs(moved_x) < MIN_PAN_PX
                       and abs(moved_y) < MIN_PAN_PX)
            if stalled:
                raise DeadPan(
                    f"panned by ({dx},{dy}) px but the map moved "
                    f"({moved_x:.0f},{moved_y:.0f}) across {shared} shared "
                    "pin(s). The gesture is not reaching the map.")

        # The child re-searches on entry, so nothing is needed here.
        sweep(device, cell, max_depth=max_depth, verify_pans=verify_pans,
              settle_min_s=settle_min_s, settle_max_s=settle_max_s,
              _depth=_depth + 1, _result=result)

    return result
