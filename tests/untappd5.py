"""Synthetic Untappd 5.x screens and a fake app that moves between them.

Node shapes, labels and bounds are copied from real `uiautomator` dumps of
Untappd 5.0.3 (Android 9, 900x1600 portrait), captured 2026-11-14. The
captures themselves stay out of the repo: they carry the emulator's current
location.

`ScreenApp` resolves each tap to the labelled node under it, so a test
exercises the same find-by-label path the sweep uses on a real device. A tap
on nothing, or on a label with no transition, is recorded and changes nothing,
which is what the real app does with a stray tap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def _wrap(nodes: str) -> str:
    return f"<?xml version='1.0' encoding='UTF-8'?><hierarchy>{nodes}</hierarchy>"


def map_nodes(place: str = "Haifa, Israel", pins: tuple[str, ...] = ()) -> str:
    """The map screen: header, tabs, the map surface, pins, card, footer."""
    pin_nodes = "".join(
        f'<node class="android.view.View" content-desc="{name}" '
        f'bounds="[{100 + i * 7},{300 + i * 9}][{154 + i * 7},{362 + i * 9}]"/>'
        for i, name in enumerate(pins))
    return (
        f'<node class="android.widget.Button" content-desc="{place}" clickable="true" bounds="[18,36][429,102]"/>'
        f'<node class="android.widget.TextView" text="{place}" bounds="[78,54][369,84]"/>'
        '<node class="android.widget.Button" content-desc="Filter" clickable="true" bounds="[738,36][804,102]"/>'
        '<node class="android.widget.Button" content-desc="Switch to list view" clickable="true" bounds="[816,36][882,102]"/>'
        '<node class="android.widget.Button" content-desc="Places" clickable="true" bounds="[0,117][450,186]"/>'
        '<node class="android.widget.Button" content-desc="Producers" clickable="true" bounds="[450,117][900,186]"/>'
        '<node class="android.view.View" content-desc="Google Map" clickable="true" bounds="[0,189][900,1600]"/>'
        f"{pin_nodes}"
        '<node class="android.widget.Button" content-desc="Reset location" clickable="true" bounds="[822,201][888,267]"/>'
        '<node class="android.view.ViewGroup" content-desc="Lauter, 10 HaArba\'a Street Tel Aviv, Category: Bar, Beer Store" clickable="true" bounds="[24,1258][876,1409]"/>'
        '<node class="android.widget.Button" content-desc="View Menu" clickable="true" bounds="[42,1422][858,1471]"/>'
        '<node class="android.widget.Button" content-desc="Back to Discover" clickable="true" bounds="[322,1504][579,1588]"/>'
    )


def map_screen(place: str = "Haifa, Israel", pins: tuple[str, ...] = ()) -> str:
    return _wrap(map_nodes(place, pins))


def location_sheet(place: str = "Haifa, Israel", typed: str = "") -> str:
    """The "Choose Location" sheet. It is drawn OVER the map, and the map's
    nodes stay in the dump underneath -- so the map check alone passes here."""
    field_text = typed or "City or Region"
    clear = ('<node class="android.widget.Button" content-desc="Clear" clickable="true" '
             'bounds="[828,1358][864,1394]"/>') if typed else ""
    return _wrap(
        '<node class="android.widget.Button" content-desc="Close" clickable="true" bounds="[0,0][900,1600]"/>'
        '<node class="android.view.View" text="Choose Location" bounds="[18,1267][287,1316]"/>'
        '<node class="android.widget.Button" content-desc="Close" clickable="true" bounds="[816,1259][882,1325]"/>'
        f'<node class="android.widget.EditText" text="{field_text}" clickable="true" bounds="[78,1344][810,1408]"/>'
        f"{clear}"
        '<node class="android.widget.Button" content-desc="Use Current Location, Somewhere" clickable="true" bounds="[18,1478][882,1582]"/>'
        + map_nodes(place))


FILTER_PANEL = _wrap(
    '<node class="android.widget.Button" content-desc="Close" clickable="true" bounds="[18,36][84,102]"/>'
    '<node class="android.view.View" text="Filter" bounds="[102,52][163,89]"/>'
    '<node class="android.widget.Button" content-desc="Clear" clickable="true" bounds="[768,36][882,102]"/>'
    '<node class="android.widget.Button" content-desc="Filter by Category" clickable="true" bounds="[18,135][882,210]"/>'
    '<node class="android.widget.Button" content-desc="Filter by Menu Offering" clickable="true" bounds="[18,210][882,285]"/>'
    '<node class="android.widget.CheckBox" content-desc="Has Local Badge" clickable="true" bounds="[18,285][882,360]"/>'
    '<node class="android.widget.Button" content-desc="Show Results" clickable="true" bounds="[18,1513][882,1589]"/>'
)

DISCOVER = _wrap(
    '<node class="android.widget.Button" content-desc="Search" clickable="true" bounds="[18,36][84,102]"/>'
    '<node class="android.view.View" content-desc="Nearby Places" bounds="[18,300][882,360]"/>'
    '<node class="android.widget.Button" content-desc="Map" clickable="true" bounds="[389,1425][512,1509]"/>'
    '<node class="android.widget.Button" content-desc="Discover" clickable="true" bounds="[180,1516][360,1600]"/>'
)

ROW_TOP = 252
ROW_HEIGHT = 75


def category_panel(rows: list[tuple[str, int, bool]], first: int = 0) -> str:
    """The category list, as `"Bar, 21"` rows -- no trailing comma in 5.x.
    `first` is how far the list has scrolled.

    The bulk button is a toggle: `Deselect All` while anything is ticked,
    `Select All` once nothing is. Found live: a guard that knew only the
    first label refused the panel the moment the driver pressed it."""
    bulk = "Deselect All" if any(on for _, _, on in rows) else "Select All"
    nodes = "".join(
        f'<node class="android.widget.CheckBox" content-desc="{name}, {count}" '
        f'checked="{str(on).lower()}" clickable="true" '
        f'bounds="[18,{ROW_TOP + i * ROW_HEIGHT}][882,{ROW_TOP + (i + 1) * ROW_HEIGHT}]"/>'
        for i, (name, count, on) in enumerate(rows[first:first + 16]))
    return _wrap(
        '<node class="android.widget.Button" content-desc="Back" clickable="true" bounds="[18,36][84,102]"/>'
        '<node class="android.view.View" text="Category" bounds="[102,52][211,89]"/>'
        '<node class="android.widget.Button" content-desc="Apply" clickable="true" bounds="[763,36][882,102]"/>'
        '<node class="android.widget.EditText" text="Search" clickable="true" bounds="[78,118][882,182]"/>'
        f'<node class="android.widget.Button" content-desc="{bulk}" clickable="true" bounds="[36,207][870,240]"/>'
        + nodes)


def _hit(xml: str, x: int, y: int) -> str | None:
    """The label of the smallest clickable node under (x, y)."""
    best: tuple[int, str] | None = None
    for m in re.finditer(r"<node ([^>]*)/>", xml):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(1)))
        if attrs.get("clickable") != "true":
            continue
        b = _BOUNDS.match(attrs.get("bounds", ""))
        if not b:
            continue
        x1, y1, x2, y2 = (int(g) for g in b.groups())
        if x1 <= x <= x2 and y1 <= y <= y2:
            area = (x2 - x1) * (y2 - y1)
            label = attrs.get("content-desc") or attrs.get("text") or ""
            if best is None or area < best[0]:
                best = (area, label)
    return best[1] if best else None


@dataclass
class ScreenApp:
    """A fake Untappd 5.x that follows taps the way the real one does.

    `screen` is one of `map`, `location`, `filter`, `category`, `discover`, or
    anything else (an unknown screen with no controls).
    """

    screen: str = "map"
    place: str = "Haifa, Israel"
    pins: tuple[str, ...] = ("Lauter",)
    categories: list[tuple[str, int, bool]] = field(default_factory=lambda: [
        ("Bar", 21, True), ("Liquor Store", 7, True), ("Park", 7, True),
        ("Pub", 7, True), ("Hotel", 6, True)])
    focus: str = "com.untappdllc.app"
    actions: list[str] = field(default_factory=list)
    typed: str = ""
    scrolled: int = 0
    applied: list[str] | None = None
    searched: list[str] = field(default_factory=list)

    # --- the Device protocol -------------------------------------------
    def focused_package(self) -> str:
        return self.focus

    def dump(self) -> str:
        if self.screen == "map":
            return map_screen(self.place, self.pins)
        if self.screen == "location":
            return location_sheet(self.place, self.typed)
        if self.screen == "filter":
            return FILTER_PANEL
        if self.screen == "category":
            return category_panel(self.categories, self.scrolled)
        if self.screen == "discover":
            return DISCOVER
        return _wrap('<node class="android.widget.TextView" text="Something else" bounds="[1,1][2,2]"/>')

    def tap(self, x: int, y: int) -> None:
        label = _hit(self.dump(), x, y)
        self.actions.append(f"tap:{label}")
        self._on_tap(label)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int) -> None:
        self.actions.append(f"swipe({x2 - x1},{y2 - y1})")
        if self.screen == "category" and y2 < y1:
            self.scrolled = min(self.scrolled + 8, max(len(self.categories) - 1, 0))

    def type_text(self, text: str) -> None:
        self.actions.append(f"type:{text}")
        if self.screen == "location":
            self.typed = text

    def press_enter(self) -> None:
        self.actions.append("enter")
        if self.screen == "location" and self.typed:
            self.place = f"{self.typed}, Somewhere"
            self.searched.append(self.typed)
            self.screen = "map"
            self.typed = ""

    def launch(self, package: str) -> None:
        self.actions.append(f"launch:{package}")
        self.focus = package
        self.screen = "discover"

    # --- transitions ---------------------------------------------------
    def _on_tap(self, label: str | None) -> None:
        s = self.screen
        if s == "map" and label == self.place:
            self.screen = "location"
        elif s == "map" and label == "Filter":
            self.screen = "filter"
        elif s == "location" and label == "Clear":
            self.typed = ""
        elif s == "location" and label == "Close":
            self.screen = "map"
        elif s == "filter" and label == "Filter by Category":
            self.screen, self.scrolled = "category", 0
        elif s == "filter" and label in ("Show Results", "Close"):
            self.screen = "map"
        elif s == "category" and label == "Deselect All":
            self.categories = [(n, c, False) for n, c, _ in self.categories]
        elif s == "category" and label == "Select All":
            self.categories = [(n, c, True) for n, c, _ in self.categories]
        elif s == "category" and label == "Apply":
            self.applied = [n for n, _, on in self.categories if on]
            self.screen = "filter"
        elif s == "category" and label == "Back":
            self.screen = "filter"
        elif s == "category" and label and re.fullmatch(r".+, \d+", label):
            name = label.rsplit(", ", 1)[0]
            self.categories = [(n, c, (not on) if n == name else on)
                               for n, c, on in self.categories]
        elif s == "discover" and label == "Map":
            self.screen = "map"
