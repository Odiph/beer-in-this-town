"""Put the sweep on the ground: pixels to coordinates, viewports to cells.

The sweep subdivides in *pixels* -- quarter-viewport pans -- which works and
tells you nothing about where it has been. Without ground coordinates
coverage cannot be reported, a resumed sweep cannot tell which ground it
already covered, and venues arrive with names and screen positions but no
location.

All three need one number: the map's scale, in metres per pixel.

**Panning cannot supply it.** A swipe reveals how many pixels the map moved,
and there is no external reference in that to convert pixels into degrees.
Calibration needs one known point, and a city search gives one for free: the
app centres the map on the city, which `geocode.py` already resolves. Pair
that centre with the measured scale and every pin in the same dump becomes a
coordinate.

The scale is **zoom-dependent**, so `CITY_ZOOM_M_PER_PX` holds only for the
zoom a city search lands on. `scale_from_known_points` refits it from any two
pins whose coordinates are known, which is how another zoom gets calibrated.

Accuracy is about 10 m in open ground and can be 100 m in a dense cluster,
because the map nudges overlapping markers apart -- `Lauter`, 11 px from
`Schnitt` on the same street, fitted 111 m from its true position while every
other pin was within 16 m. Good for a radius filter, not for an address.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .app_map import Pin
from .app_sweep import MAP_BOTTOM, MAP_TOP, SCREEN_WIDTH, Cell

# Metres per pixel at the zoom a city search lands on. Fitted on the live app
# from ten pins whose coordinates were known: longitude residuals +-6 m.
CITY_ZOOM_M_PER_PX = 10.2

# Metres per degree. Latitude is near enough constant; longitude shrinks with
# the cosine of latitude, which matters at 32 degrees (a factor of 0.85) and
# matters a great deal further north.
_M_PER_DEG_LAT = 110_540.0
_M_PER_DEG_LNG_EQUATOR = 111_320.0


@dataclass(frozen=True)
class Scale:
    """How much ground one screen pixel covers, at one zoom level."""

    m_per_px: float

    def deg_lat(self, pixels: float) -> float:
        return pixels * self.m_per_px / _M_PER_DEG_LAT

    def deg_lng(self, pixels: float, at_lat: float) -> float:
        metres = _M_PER_DEG_LNG_EQUATOR * math.cos(math.radians(at_lat))
        return pixels * self.m_per_px / metres


def _map_centre() -> tuple[float, float]:
    return SCREEN_WIDTH / 2, (MAP_TOP + MAP_BOTTOM) / 2


def to_latlng(pin: Pin, centre: tuple[float, float], scale: Scale
              ) -> tuple[float, float]:
    """Where on earth a pin is, given where the map is centred.

    Screen `y` grows downward while latitude grows northward, so the sign is
    flipped. Getting that backwards puts every venue in the wrong half of the
    city, and plausibly so -- the corpus would look fine.
    """
    cx, cy = _map_centre()
    lat = centre[0] - scale.deg_lat(pin.y - cy)
    lng = centre[1] + scale.deg_lng(pin.x - cx, centre[0])
    return lat, lng


def cell_for_viewport(centre: tuple[float, float], scale: Scale) -> Cell:
    """The ground the current viewport covers, as a `Cell`.

    This is what makes `Cell.quarters()` mean something: before it, the
    sweep's subdivision was pixel arithmetic that no one could check against
    a map.
    """
    half_w = SCREEN_WIDTH / 2
    half_h = (MAP_BOTTOM - MAP_TOP) / 2
    dlat = scale.deg_lat(half_h)
    dlng = scale.deg_lng(half_w, centre[0])
    return Cell(left=centre[1] - dlng, right=centre[1] + dlng,
                bottom=centre[0] - dlat, top=centre[0] + dlat)


def centre_from_known_points(
    points: list[tuple[Pin, float, float]], scale: Scale,
) -> tuple[float, float]:
    """Where the map is actually centred, from pins whose coordinates are known.

    **Do not assume the searched city's centroid.** Measured: after searching
    `"Tel Aviv"`, every known pin was displaced by an almost identical
    1085 m from where the geocoded centroid predicted -- a constant offset,
    not scatter, and the scale itself refitted to exactly the expected
    10.20 m/px. The app centres on its own idea of the place, not on
    Nominatim's.

    A constant offset is the kindest possible error here: it is invisible in
    the venue list, consistent enough to look correct, and would silently put
    every cell boundary a kilometre from where the coverage report claims.

    Averaging over all points absorbs the marker-nudging noise that displaced
    `Lauter` by 111 m.
    """
    if not points:
        raise ValueError("need at least one known point to locate the map")
    cx, cy = _map_centre()
    # Latitude first: `to_latlng` scales longitude by the *centre's*
    # latitude, so solving longitude with each pin's own latitude would not
    # be its inverse, and the error would grow with distance from the centre.
    lats = [lat + scale.deg_lat(pin.y - cy) for pin, lat, _ in points]
    centre_lat = sum(lats) / len(lats)
    lngs = [lng - scale.deg_lng(pin.x - cx, centre_lat)
            for pin, _, lng in points]
    return centre_lat, sum(lngs) / len(lngs)


def scale_from_known_points(
    points: list[tuple[Pin, float, float]],
) -> Scale:
    """Refit the scale from pins whose real coordinates are known.

    Two separated points are the minimum that determines a scale. Fewer, or
    two in the same place, and there is nothing to fit -- returning a
    fabricated number would put every venue somewhere plausible and wrong,
    which is the failure this module exists to avoid.

    Uses the widest-separated pair, since a short baseline magnifies the
    marker-nudging error that displaced `Lauter` by 111 m.
    """
    if len(points) < 2:
        raise ValueError(
            f"need 2 known points to fit a scale, got {len(points)}")

    best: tuple[float, float] | None = None
    for i, (pin_a, lat_a, lng_a) in enumerate(points):
        for pin_b, lat_b, lng_b in points[i + 1:]:
            px = math.hypot(pin_b.x - pin_a.x, pin_b.y - pin_a.y)
            if px < 1:
                continue
            mid_lat = (lat_a + lat_b) / 2
            dy = (lat_b - lat_a) * _M_PER_DEG_LAT
            dx = ((lng_b - lng_a)
                  * _M_PER_DEG_LNG_EQUATOR * math.cos(math.radians(mid_lat)))
            metres = math.hypot(dx, dy)
            if best is None or px > best[0]:
                best = (px, metres / px)

    if best is None:
        raise ValueError(
            "known points are all at the same screen position; "
            "cannot fit a scale from a zero baseline")
    return Scale(m_per_px=best[1])
