"""Decide which of the app's categories are drinking places, and drive the
filter panel that selects them.

**Why this exists.** A search returns at most about 60 venues, and Untappd's
index is not a drinking index. One Tel Aviv viewport spent its slots on
`Ramat Gan National Park`, `Crowne Plaza`, `Expo Tel Aviv` and `Yad-Eliyahu
Arena`. Measured worse: of the four venues found only by deep subdivision
that cleared a 200 check-in bar, one was a **supermarket chain** and one was
a **highway**. Both had real check-in counts, because people check in beer
wherever they drink it. Numbers attached to a place do not make it a venue.

**Why it cannot be done afterwards.** Pins carry a name and a position and
nothing else. The category is only available in the filter panel, so the
filter has to be set *before* the search that fills the cap.

The panel is scrollable and its rows shift, so `plan_category_taps` works
from what a given dump actually shows rather than from fixed coordinates.
The caller taps, re-dumps, and asks again until the plan comes back empty.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .app_map import Category
from .classify import KEEP_CATEGORIES

# Categories that mean "a craft-beer venue could be here". Matched as whole
# phrases against the comma-separated list the app gives, so `Hotel Bar` is
# kept while `Hotel` is not -- a distinction substring matching cannot make,
# and the one that decides whether a hotel lobby is a beer destination.
#
# Defined once, in `classify`: `filter` applies the same vocabulary to the
# enriched rows, so what the sweep collects and what the map keeps agree.
# Wine bars, cocktail bars, wineries and distilleries are deliberately absent.
DRINKING_CATEGORIES = KEEP_CATEGORIES

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# `"Beer Bar, 2, "` -- the same shape `app_map.categories_in` parses. Both
# must strip the count identically or the two dictionaries key differently
# and every lookup misses.
_LABEL = re.compile(r"^(?P<name>.+?),\s*\d+,")


class CategoryPanelError(RuntimeError):
    """The dump is not the category filter panel.

    Raised rather than returning an empty plan: tapping blind through a
    screen that is not the panel toggles whatever happens to sit at those
    coordinates.
    """


@dataclass(frozen=True)
class CategoryTap:
    """A row to toggle, and where to touch it."""

    name: str
    x: int
    y: int


def is_drinking_category(label: str) -> bool:
    """Does this category label describe somewhere you drink?

    The app hands out several categories at once -- `Bar, Diner`,
    `Beer Store, Hobby Shop, Building` -- and any drinking part qualifies the
    venue. A venue that is a bar *and* a diner is still a bar.
    """
    parts = [p.strip().casefold() for p in label.split(",")]
    return any(p in DRINKING_CATEGORIES for p in parts)


def _require_panel(xml: str) -> None:
    if "Filter by Category" not in xml:
        raise CategoryPanelError(
            "no `Filter by Category` heading -- this is not the category "
            "panel, and tapping through it would toggle whatever is there.")


def plan_category_taps(categories: dict[str, Category],
                       xml: str) -> list[CategoryTap]:
    """Which rows need toggling so that exactly the drinking ones are on.

    Returns taps for rows whose state disagrees with what we want: a wanted
    row that is off, or an unwanted row that is on. An empty list means the
    visible rows are already correct -- not that nothing happened.

    The caller re-dumps after tapping and asks again, because the panel
    scrolls and a row's position moves.
    """
    _require_panel(xml)

    bounds: dict[str, tuple[int, int, int, int]] = {}
    for m in re.finditer(
            r'content-desc="([^"]+)"[^>]*?bounds="(\[-?\d+,-?\d+\]\[-?\d+,-?\d+\])"',
            xml):
        raw = m.group(1)
        named = _LABEL.match(raw)
        label = named.group("name").strip() if named else raw.rstrip(", ").strip()
        b = _BOUNDS.match(m.group(2))
        if b:
            bounds[label] = tuple(int(g) for g in b.groups())  # type: ignore[assignment]

    taps: list[CategoryTap] = []
    for name, category in categories.items():
        wanted = is_drinking_category(name)
        if wanted == category.checked:
            continue
        box = bounds.get(name)
        if box is None:
            # The row is in the parse but its bounds are not; tapping a
            # guessed position is how you toggle the wrong category.
            continue
        x1, y1, x2, y2 = box
        taps.append(CategoryTap(name=name, x=(x1 + x2) // 2, y=(y1 + y2) // 2))
    return taps
