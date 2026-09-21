"""Sweep a city out of the app's map by recursive subdivision.

`app_map.py` reads one screen. This walks many of them.

**The rule the sweep turns on.** A search returns a result set capped at
about 58 venues, so a cell that comes back at the cap was truncated and hides
more; a cell that comes back under it is taken as complete. Measured on the
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
from dataclasses import dataclass, field
from typing import Protocol

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


def _refresh(device: Device) -> None:
    """Re-query for the current viewport.

    Tapped by position because the button's bounds are stable within a
    session; `REFRESH_DESC` records what it is so a future reader can find it
    by description instead if the layout moves.
    """
    device.tap(855, 315)


def _pan(device: Device, dx: int, dy: int) -> None:
    cx, cy = SCREEN_WIDTH // 2, (MAP_TOP + MAP_BOTTOM) // 2
    device.swipe(cx, cy, cx + dx, cy + dy, PAN_MS)


def _harvest_screen(device: Device) -> list[Pin]:
    xml = device.dump()
    require_map_screen(xml)
    return pins_in(xml)


def sweep(device: Device, cell: Cell, max_depth: int = 3,
          verify_pans: bool = False, _depth: int = 0,
          _result: SweepResult | None = None) -> SweepResult:
    """Harvest `cell`, subdividing wherever the result set was truncated.

    `max_depth` bounds the recursion. A dense centre can stay at the cap
    however far it is divided, and the sweep must stop and *say* it stopped
    rather than loop -- `hit_depth_limit` is that admission.

    `verify_pans` measures that the map actually moved between cells. It
    costs an extra dump per pan, which is why it is opt-in, and it is the
    difference between a sweep that covers a city and one that harvests the
    same rectangle repeatedly.
    """
    result = _result if _result is not None else SweepResult()

    if _depth == 0:
        _require_app(device)

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
        _pan(device, dx, dy)

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

        # Without this the child shows a slice of the parent's result set.
        _refresh(device)
        sweep(device, cell, max_depth=max_depth, verify_pans=verify_pans,
              _depth=_depth + 1, _result=result)

    return result
