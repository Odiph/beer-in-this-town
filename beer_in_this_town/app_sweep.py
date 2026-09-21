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
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

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

# Discover -> "View Map". Located by its text in the dump, never tapped by a
# remembered position: recovery runs precisely when the screen is not what
# was expected, which is the one moment a remembered position is least safe.
VIEW_MAP_LABEL = "View Map"

# How many looks a relaunch gets before the screen is declared wrong. At
# half a settle apart that is roughly 20-30 s, well past a measured cold start.
RELAUNCH_POLLS = 8
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
# A `Refresh search` re-queries the network, and a dump taken too early
# catches a half-drawn map -- which reads as a thinner city, not as an error.
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
                    "lat": v.lat, "lng": v.lng} for v in result.venues],
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
    for _ in range(RELAUNCH_POLLS):
        xml = device.dump()
        try:
            require_map_screen(xml)
            return
        except WrongScreen:
            pass
        target = _find_label(xml, VIEW_MAP_LABEL)
        if target is not None:
            break
        _settle(settle_min_s / 2, settle_max_s / 2)
    if target is None:
        raise WrongScreen(
            f"relaunched, but there is no {VIEW_MAP_LABEL!r} on screen to "
            "reach the map from. Refusing to tap blind; open Discover -> "
            "View Map by hand and re-run.")
    device.tap(*target)
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
    panel_lo = min(PANEL_SETTLE_MIN_S, settle_min_s)
    panel_hi = min(PANEL_SETTLE_MAX_S, settle_max_s)

    device.tap(*FILTERS_BUTTON)
    _settle(panel_lo, panel_hi)
    device.tap(*CATEGORY_ROW)
    _settle(panel_lo, panel_hi)

    kept: set[str] = set()
    seen: set[str] = set()
    for _ in range(MAX_CATEGORY_PASSES):
        xml = device.dump()
        cats = categories_in(xml)
        kept.update(n for n, c in cats.items() if c.checked)
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

    device.tap(*APPLY_BUTTON)
    _settle(panel_lo, panel_hi)
    # This one issues a query, so it gets a search's patience.
    device.tap(*SHOW_RESULTS_BUTTON)
    _settle(settle_min_s, settle_max_s)

    return sorted(n for n in seen if is_drinking_category(n))


def _harvest_screen(device: Device) -> list[Pin]:
    xml = device.dump()
    require_map_screen(xml)
    return pins_in(xml)


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
            lat, lng = camera.locate(pin) if camera else (None, None)
            result.venues.append(
                Venue(name=pin.name, x=pin.x, y=pin.y, lat=lat, lng=lng))

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

    for dx, dy in ((-step_x, -step_y), (step_x, -step_y),
                   (-step_x, step_y), (step_x, step_y)):
        before = pins if verify_pans else None
        _pan(device, dx, dy, pins)
        _settle(settle_min_s, settle_max_s)

        if not verify_pans:
            _follow(camera, dx, dy)
        else:
            after = _harvest_screen(device)
            moved_x, moved_y, shared = pin_displacement(before or [], after)
            measured = (moved_x is not None and moved_y is not None
                        and shared >= MIN_SHARED_FOR_PAN_CHECK)
            # Believe the measurement when there is enough overlap to make
            # one; otherwise the request is the best estimate there is.
            if measured:
                _follow(camera, moved_x, moved_y)
            else:
                _follow(camera, dx, dy)
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

            # Close the loop. The fling overshoots by up to 27%, and that
            # error was being measured and then ignored, so it accumulated
            # across a sweep and shifted every later cell off its target.
            if (moved_x is not None and moved_y is not None
                    and shared >= MIN_SHARED_FOR_PAN_CHECK):
                err_x, err_y = dx - moved_x, dy - moved_y
                if abs(err_x) > PAN_TOLERANCE_PX or abs(err_y) > PAN_TOLERANCE_PX:
                    log.info("Pan landed (%.0f,%.0f) px off target; correcting.",
                             err_x, err_y)
                    _pan(device, int(err_x), int(err_y), after)
                    _follow(camera, int(err_x), int(err_y))
                    _settle(settle_min_s, settle_max_s)

        # The child re-searches on entry, so nothing is needed here.
        sweep(device, cell, max_depth=max_depth, min_depth=min_depth,
              verify_pans=verify_pans,
              city=city, filter_drinking=filter_drinking, camera=camera,
              settle_min_s=settle_min_s, settle_max_s=settle_max_s,
              _depth=_depth + 1, _result=result)

    return result
