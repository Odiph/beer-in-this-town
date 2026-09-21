"""The census step: sweep a city out of the Untappd app and put it on the ground.

This is how a city's venues are found. The web search cannot do it: it
matches venue *names*, not places (a Tel Aviv search returned a grill in
Encino and a kitchen in Miami Beach), and signed out it stops at five. The
app's map is a real geographic search, so the pipeline is:

    position the map  ->  sweep (app_sweep)  ->  calibrate (app_calibrate)
                      ->  rows (app_export)  ->  CSV / map files

**Positioning.** Either search the city by name in the app and take the
geocoder's centre for it, or (`here`) tap `Reset location` and take the
device's own GPS fix. The geocoder's centre is known to sit about a kilometre
from where the app actually centres; that is fine, because calibration fits
the shift along with the scale. The GPS path is the one measured live (18 m
median after calibration).

**Calibration is not optional.** The app picks its own zoom, so the assumed
metres-per-pixel is only a starting guess. `calibrate` matches swept names to
OpenStreetMap and refuses when the fit cannot be trusted; this module lets
that refusal through rather than writing coordinates it knows are wrong.

Everything the sweep does not know (Untappd id, category, check-in counts)
travels as unknown. It is never written as zero.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .agent_io import Envelope
from .app_calibrate import Calibration, calibrate
from .app_export import to_venues
from .app_geo import CITY_ZOOM_M_PER_PX, Camera, Scale
from .app_sweep import (
    JOURNAL_MAX_AGE_H,
    Device,
    SweepResult,
    _settle,
    discard_journal,
    ensure_map_screen,
    journal_path,
    load_finished,
    mark_complete,
    search_city,
    sweep,
)
from .config import SWEEP_CSV, Settings, cli_arg, stage_path
from .export import write_csv, write_geojson, write_gpx, write_kml
from .geo import haversine_km
from .geocode import geocode_place
from .models import Venue
from .overpass import nearby_venues

log = logging.getLogger(__name__)

# Depth recommendation from the measurements in docs/HARVESTING.md: never
# depth 0 (a city-state lands at country zoom and looks complete at 13
# venues), one forced split, adaptive below that.
DEFAULT_MIN_DEPTH = 1
DEFAULT_MAX_DEPTH = 3

# The OSM census used to calibrate must cover every swept venue. The radius is
# taken from the sweep's own spread, padded, and clamped so a runaway
# coordinate cannot turn one Overpass query into a country.
OSM_MARGIN_KM = 1.0
MIN_OSM_RADIUS_KM = 3.0
MAX_OSM_RADIUS_KM = 25.0

RESET_LOCATION_DESC = "Reset location"
_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

GEO_SOURCES = ("geocoder", "gps")


class CityNotFound(RuntimeError):
    """The geocoder has no match for the city, so there is no centre."""


class NoLocationControl(RuntimeError):
    """`Reset location` is not on screen, so the GPS centre cannot be used."""


@dataclass(frozen=True)
class Census:
    """A swept, calibrated city, and everything needed to judge it."""

    venues: list[Venue]
    sweep: SweepResult
    calibration: Calibration
    centre: tuple[float, float]
    centre_source: str
    osm_radius_km: float


def _find_desc(xml: str, desc: str) -> tuple[int, int] | None:
    root = ET.fromstring(xml)
    for node in root.iter():
        if (node.get("content-desc") or "").strip() == desc:
            m = _BOUNDS.match(node.get("bounds") or "")
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def centre_on_device(device: Device, settle_min_s: float,
                     settle_max_s: float) -> tuple[float, float]:
    """Centre the map on the device's GPS fix and return that fix.

    Located by content-desc, never by a remembered position. `location()`
    raises rather than guessing when the emulator has no fix.
    """
    target = _find_desc(device.dump(), RESET_LOCATION_DESC)
    if target is None:
        raise NoLocationControl(
            f"No {RESET_LOCATION_DESC!r} control on the map screen, so the "
            "device position cannot centre the sweep.")
    device.tap(*target)
    _settle(settle_min_s, settle_max_s)
    return device.location()  # type: ignore[attr-defined]


def osm_radius_km(venues: list, centre: tuple[float, float]) -> float:
    """How far the calibration census must reach to cover the sweep."""
    distances = [haversine_km(centre, (v.lat, v.lng)) for v in venues
                 if v.lat is not None and v.lng is not None]
    reach = max(distances, default=0.0) + OSM_MARGIN_KM
    return min(max(reach, MIN_OSM_RADIUS_KM), MAX_OSM_RADIUS_KM)


def census(device: Device, city: str, s: Settings, *, here: bool = False,
           min_depth: int = DEFAULT_MIN_DEPTH,
           max_depth: int = DEFAULT_MAX_DEPTH,
           locate_city: Callable[[str, Settings], tuple[float, float] | None]
           = geocode_place,
           known_near: Callable = nearby_venues,
           settle_min_s: float | None = None,
           settle_max_s: float | None = None,
           fresh: bool = False) -> Census:
    """Sweep `city` from the app and return calibrated rows.

    Raises `CityNotFound`, `NoLocationControl`, `CalibrationFailed`,
    `GeocoderUnavailable`, `OverpassUnavailable`, and the app errors
    (`AdbUnavailable`, `WrongScreen`, `DeadPan`). None of them is caught
    here: each wants a different remedy, and the CLI maps them.
    """
    settle = {} if settle_min_s is None else {
        "settle_min_s": settle_min_s, "settle_max_s": settle_max_s}
    lo = settle.get("settle_min_s", 5.0)
    hi = settle.get("settle_max_s", 8.0)

    if fresh:
        discard_journal(city)
    else:
        discard_journal(city, only_if_older_than_h=JOURNAL_MAX_AGE_H)
    finished = load_finished(city)
    if finished is not None:
        # Every cell was already walked; only the steps after it failed.
        log.info("A complete sweep of %s from the last %.0fh has %d venue(s); "
                 "placing it without sweeping again (--fresh to re-sweep).",
                 city, JOURNAL_MAX_AGE_H, len(finished.result.venues))
        result, centre = finished.result, finished.centre
        source = finished.centre_source
    else:
        if here:
            ensure_map_screen(device, **settle)
            centre = centre_on_device(device, lo, hi)
            source = "gps"
        else:
            # Geocode before touching the device: a city the geocoder cannot
            # place would otherwise cost a whole sweep before it failed.
            centre = locate_city(city, s)
            if centre is None:
                raise CityNotFound(f"The geocoder has no match for {city!r}.")
            search_city(device, city, **settle)
            source = "geocoder"

        camera = Camera(centre=centre,
                        scale=Scale(m_per_px=CITY_ZOOM_M_PER_PX))
        result = sweep(device, camera.viewport(), max_depth=max_depth,
                       min_depth=min_depth, verify_pans=True, city=city,
                       filter_drinking=True, camera=camera, **settle)
        mark_complete(city, result, centre, source)

    radius = osm_radius_km(result.venues, centre)
    known = [(o.name, o.lat, o.lng) for o in known_near(centre, radius, s)]
    fixed, cal = calibrate(result.venues, known, centre)

    return Census(venues=to_venues(SweepResult(venues=fixed), city),
                  sweep=result, calibration=cal, centre=centre,
                  centre_source=source, osm_radius_km=radius)


def write_census(c: Census, city: str, title: str,
                 formats: tuple[str, ...] = ()) -> tuple[Path, dict[str, str]]:
    """data/<slug>/1_sweep.csv always; 1_sweep.<fmt> only when asked for.

    A re-run overwrites: the stage file is the sweep's current answer for the
    city, and the journal (not this file) is what carries a sweep across an
    interruption.
    """
    csv_target = stage_path(city, SWEEP_CSV)
    csv_target.parent.mkdir(parents=True, exist_ok=True)
    csv_path = write_csv(c.venues, csv_target)
    base = csv_target.with_suffix("")
    written: dict[str, str] = {}
    if "kml" in formats:
        written["kml"] = str(write_kml(c.venues, base.with_suffix(".kml"), title))
    if "geojson" in formats:
        written["geojson"] = str(write_geojson(c.venues,
                                               base.with_suffix(".geojson")))
    if "gpx" in formats:
        written["gpx"] = str(write_gpx(c.venues, base.with_suffix(".gpx"), title))
    return csv_path, written


def census_envelope(c: Census, city: str, list_name: str, csv_path: Path,
                    written: dict[str, str]) -> Envelope:
    """Report the census, including everything the sweep doubts about itself."""
    r = c.sweep
    cal = c.calibration
    unlocated = [v.ref.name for v in c.venues if not v.has_coords]

    warnings = list(r.warnings)
    if r.hit_depth_limit:
        warnings.append(
            "At least one cell was still at the result-set cap when the depth "
            "limit stopped it, so this area holds more venues than were "
            "collected. Re-run with a higher --max-depth to go further.")
    if unlocated:
        warnings.append(
            f"{len(unlocated)} venue(s) have no coordinates: "
            f"{', '.join(unlocated[:5])}")
    warnings.append(
        "Check-in counts, category and Untappd id are unknown for swept "
        "venues (written as n/a, never 0) until they are joined to their "
        "venue pages.")

    return Envelope(
        command="sweep",
        ok=True,
        data={
            "city": city,
            "venues": len(c.venues),
            "located": len(c.venues) - len(unlocated),
            "csv": str(csv_path),
            "maps": written,
            "centre": list(c.centre),
            "centre_source": c.centre_source,
            "cells_visited": r.cells_visited,
            "truncated_cells": r.truncated_cells,
            "skipped_cells": r.skipped_cells,
            "hit_depth_limit": r.hit_depth_limit,
            "calibration": {
                "m_per_px": round(CITY_ZOOM_M_PER_PX * cal.scale, 2),
                "rotation_deg": round(cal.rotation_deg, 2),
                "median_residual_m": round(cal.median_residual_m, 1),
                "osm_radius_km": round(c.osm_radius_km, 1),
                "inliers": list(cal.inliers),
                "dropped": list(cal.dropped),
            },
            "journal": str(journal_path(city)),
        },
        warnings=warnings,
        next_actions=[
            f'python -m beer_in_this_town enrich --city {cli_arg(city)} --json'],
        hints=[
            "Next the swept names are matched to their Untappd venue pages "
            "(enrich), filtered to beer venues (filter) and exported (export). "
            "None of them touches an account.",
            *([f'The results are meant for the Google Maps list "{list_name}". '
               f"Create it by hand first (Saved -> New list)."]
              if list_name else []),
        ],
    )
