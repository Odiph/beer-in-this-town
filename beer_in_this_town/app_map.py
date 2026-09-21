"""Read the Untappd app's map screen out of a `uiautomator` dump.

This is the geographic surface Untappd's website does not have. `untappd.com`
has no public geo venue search at all -- its `/search` ignores `lat`, `lng`
and `radius` outright, and the only geo endpoint it exposes
(`/nearby/nearby_events_markup`) is events-only and returned nothing in every
city tested. The app's map does have one, and this module reads it.

**Why the pins and not the list.** The map screen has a list view beside it,
and the list is a trap: measured in one viewport, the list showed **10 rows
while the map held 58 pins**, and the 10 were a subset of the 58. Every pin
is an accessible node -- a `View` whose `content-desc` is the venue name and
whose `bounds` give its position -- so one dump enumerates the viewport with
no scrolling, no pagination and no tapping.

**The model of the surface**, measured 2026-09-21, which explains behaviour
that looks contradictory until you have it:

- A *search* produces a result set. `Refresh search`, or typing a city into
  the search box, is what creates one.
- The category filter's counts describe **the result set**.
- The map draws only the part of the result set inside the **viewport**.
- Panning re-draws; it does **not** re-query. A pin count falling after a pan
  is clipping, not a smaller city.

**The result set is capped at about 58.** Two independent Tel Aviv searches
both returned exactly 58 pins while Haifa returned 37. So a cell that comes
back at the cap is truncated and must be subdivided, and a cell under it is
probably complete. That is the whole saturation rule, and it costs one dump.

**Everything here is a pure function over a dump.** That is deliberate: the
expensive part of this work by hand was a model reading screenshots to decide
what had happened, and every one of those judgements is an assertion in
`tests/test_app_map.py` instead. At runtime this costs no tokens.

Two failures during the research are the reason `require_map_screen` exists:
a harvest captured the BlueStacks launcher and reported its five nodes as
data, and another read a dead scroll as a finished list. Neither raised.
Both returned believable numbers, which is the worst shape a failure takes
here -- a short corpus is indistinguishable from a complete one.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# Measured: two independent Tel Aviv searches both returned exactly 58 unique
# pins; Haifa returned 37. Treat >= this as "there is more here than you were
# shown" rather than as a count.
#
# It is a **threshold, not a hard ceiling**: a live sweep saw cells return 59
# and 60. So the comparison is `>=`, and a count a little above this is
# normal rather than a sign something is wrong.
RESULT_SET_CAP = 58

# Nodes on the map that carry a `content-desc` and are not venues. Without
# this the sweep harvests the toolbar: `Google Map` and `Refresh search` look
# exactly like pins to a naive reader.
_CHROME = frozenset({
    "Back", "filters", "Switch to list view", "Switch to map view",
    "VENUES", "BREWERIES", "Google Map", "My Location", "Reset location",
    "Refresh search", "View",
})

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# `"Beer Bar, 2, "` -- name, count, trailing comma. The count is absent on a
# row clipped by the screen edge.
_CATEGORY = re.compile(r"^(?P<name>.+?),\s*(?P<count>\d+),")


class WrongScreen(RuntimeError):
    """The dump is not the app's map view.

    Raised rather than returning an empty list, because "no pins" and "not
    looking at the map" want opposite responses and are indistinguishable
    from the output alone.
    """


@dataclass(frozen=True)
class Pin:
    """A venue marker. `y` is the *bottom* of the bounds: that is the tip of
    the marker, which is what sits on the venue's actual coordinates."""

    name: str
    x: int
    y: int


@dataclass(frozen=True)
class Category:
    """A row of the category filter. `count` is `None` when the row was
    clipped by the screen edge and never rendered its number -- which is
    "unknown", not zero, and must not silently shrink a census."""

    name: str
    count: int | None
    checked: bool


@dataclass(frozen=True)
class Transform:
    """Pixels to degrees. The map is locally linear, so one regression per
    axis is enough -- fitted live to about +-6 m in longitude.

    Zoom-dependent: re-fit whenever the zoom level changes.
    """

    lng_per_x: float
    lng_at_zero: float
    lat_per_y: float
    lat_at_zero: float

    def to_latlng(self, x: float, y: float) -> tuple[float, float]:
        return (self.lat_per_y * y + self.lat_at_zero,
                self.lng_per_x * x + self.lng_at_zero)


def _root(xml: str) -> ET.Element:
    try:
        return ET.fromstring(xml)
    except ET.ParseError as exc:
        raise WrongScreen(f"dump is not parseable XML: {exc}") from exc


def _centre_and_tip(node: ET.Element) -> tuple[int, int] | None:
    m = _BOUNDS.match(node.get("bounds") or "")
    if not m:
        return None
    x1, _y1, x2, y2 = (int(g) for g in m.groups())
    return (x1 + x2) // 2, y2


def require_map_screen(xml: str) -> None:
    """Refuse anything that is not the map view.

    Checks for the Google Map surface *and* the map-only `Switch to list
    view` control. The second matters: the list view is also the app, also
    full of venues, and harvesting it while believing it is the map is how a
    viewport of 58 gets reported as 10.
    """
    root = _root(xml)
    descs = {(n.get("content-desc") or "").strip() for n in root.iter()}
    if "Google Map" not in descs:
        raise WrongScreen(
            "no `Google Map` node -- this is not the map view. "
            f"Found {len(descs)} described node(s).")
    if "Switch to list view" not in descs:
        raise WrongScreen(
            "`Google Map` is present but `Switch to list view` is not -- "
            "this looks like the list view, not the map.")


def pins_in(xml: str) -> list[Pin]:
    """Every venue marker in the dump, in the order the app drew them.

    Order is kept because the leading block is the *verified* venues -- the
    `"Tel Aviv"` search led with Lauter, Ursa, Berlin Florentin and Schnitt,
    four of the eight verified venues. Beyond that block the order is not
    understood: tested against check-in counts (62% concordant) and distance
    from the search centre (38%), neither of which is a rule. So the leading
    block is meaningful and the tail is not -- do not truncate on it.
    """
    root = _root(xml)
    pins: list[Pin] = []
    seen: set[str] = set()
    for node in root.iter():
        if (node.get("class") or "").split(".")[-1] != "View":
            continue
        desc = (node.get("content-desc") or "").strip()
        if not desc or desc in _CHROME:
            continue
        # Every pin's description ends in a full stop. It is not part of the
        # name, and leaving it on breaks every join downstream.
        name = desc.rstrip(".").strip()
        if not name or name in seen:
            continue
        point = _centre_and_tip(node)
        if point is None:
            continue
        seen.add(name)
        pins.append(Pin(name=name, x=point[0], y=point[1]))
    return pins


def categories_in(xml: str) -> dict[str, Category]:
    """The category filter's rows, with the count each reports.

    The counts describe the **result set**, not the viewport, so they will
    disagree with the pin count whenever the map is showing part of a wider
    result. Measured: with `Bar` alone selected the filter said 6 and the map
    drew 3, because three of them were off-screen.
    """
    root = _root(xml)
    out: dict[str, Category] = {}
    for node in root.iter():
        if not (node.get("class") or "").endswith("CheckBox"):
            continue
        desc = (node.get("content-desc") or "").strip()
        if not desc:
            continue
        checked = node.get("checked") == "true"
        m = _CATEGORY.match(desc)
        if m:
            out[m.group("name").strip()] = Category(
                name=m.group("name").strip(),
                count=int(m.group("count")), checked=checked)
        else:
            # Clipped by the screen edge before its count rendered.
            name = desc.rstrip(", ").strip()
            out[name] = Category(name=name, count=None, checked=checked)
    return out


def is_truncated(pin_count: int) -> bool:
    """Did this cell hit the result-set cap, and therefore hide venues?

    Zero is not a truncation. An area with no venues is a real answer about a
    real place, and treating it as "subdivide further" would recurse forever
    over empty countryside.
    """
    return pin_count >= RESULT_SET_CAP


def pin_displacement(
    before: list[Pin], after: list[Pin],
) -> tuple[float | None, float | None, int]:
    """How far the map moved, measured from the pins that survived the pan.

    `input swipe` is repeatable to about 3 px but flings: a 400 ms swipe of
    300 px moved 381, while 1600 ms moved 286. Rather than trust a constant,
    the sweep pans, measures here, and corrects -- which also re-calibrates
    itself across zoom levels and devices for free.

    Returns `(None, None, 0)` when no pin is present in both dumps. That is
    "cannot tell", and it must not be confused with `(0, 0)`, which is "the
    map did not move".
    """
    by_name = {p.name: p for p in before}
    pairs = [(by_name[p.name], p) for p in after if p.name in by_name]
    if not pairs:
        return None, None, 0
    dx = sum(b.x - a.x for a, b in pairs) / len(pairs)
    dy = sum(b.y - a.y for a, b in pairs) / len(pairs)
    return dx, dy, len(pairs)


def fit_transform(points: list[tuple[float, float, float, float]]) -> Transform:
    """Fit pixels to degrees from `(x, y, lat, lng)` samples.

    Two points is the minimum that determines a line; below that there is
    nothing to fit and a fabricated transform would place every venue
    somewhere plausible and wrong.
    """
    if len(points) < 2:
        raise ValueError(
            f"need at least 2 reference points to fit a transform, got {len(points)}")

    def _fit(us: list[float], vs: list[float]) -> tuple[float, float]:
        mu = sum(us) / len(us)
        mv = sum(vs) / len(vs)
        den = sum((u - mu) ** 2 for u in us)
        if den == 0:
            raise ValueError("reference points share a coordinate; cannot fit")
        slope = sum((u - mu) * (v - mv) for u, v in zip(us, vs, strict=True)) / den
        return slope, mv - slope * mu

    lng_per_x, lng_at_zero = _fit([p[0] for p in points], [p[3] for p in points])
    lat_per_y, lat_at_zero = _fit([p[1] for p in points], [p[2] for p in points])
    return Transform(lng_per_x=lng_per_x, lng_at_zero=lng_at_zero,
                     lat_per_y=lat_per_y, lat_at_zero=lat_at_zero)
