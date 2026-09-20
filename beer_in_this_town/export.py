"""CSV + KML output, and the run-to-run diff."""
from __future__ import annotations

import csv
import html as html_mod
import json
import logging
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from .config import DATA_DIR, STATE_DIR, scope_slug
from .models import CSV_FIELDS, Venue

log = logging.getLogger(__name__)

# Pre-scoping layout: one baseline for the whole machine. Read once so an
# upgrade does not look like a brand-new city, then superseded.
LEGACY_PREVIOUS_RUN = STATE_DIR / "previous_run.json"


def baseline_path(query: str) -> Path:
    """The diff baseline belongs to a city, not to the install.

    A single previous_run.json meant a London run diffed itself against
    Singapore -- every venue "new", every Singapore venue "gone" -- and then
    overwrote the Singapore history, which was the only copy.
    """
    return STATE_DIR / f"previous_run_{scope_slug(query)}.json"


def _read_baseline(query: str) -> dict[str, dict]:
    path = baseline_path(query)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if LEGACY_PREVIOUS_RUN.exists():
        log.info(
            "No baseline for %r yet; adopting the pre-scoping %s. It will be "
            "replaced by a per-city one after this run.",
            query, LEGACY_PREVIOUS_RUN.name,
        )
        return json.loads(LEGACY_PREVIOUS_RUN.read_text(encoding="utf-8"))
    return {}


def write_csv(venues: list[Venue], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" is required on Windows or every row gets a blank line after it.
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(v.to_row() for v in venues)
    log.info("Wrote %d rows -> %s", len(venues), path)
    return path


def write_kml(venues: list[Venue], path: Path, title: str) -> Path:
    """KML beats CSV for My Maps import: exact coordinates, no re-geocoding by
    Google, and a rich info-window body carrying the check-in stats."""
    pinned = [v for v in venues if v.has_coords]
    skipped = len(venues) - len(pinned)
    if skipped:
        log.warning("%d venue(s) have no coordinates and will NOT be pinned.", skipped)
    if len(pinned) > 2000:
        raise ValueError(
            f"{len(pinned)} placemarks exceeds the My Maps 2000-per-layer limit. "
            "My Maps truncates silently -- split into multiple files instead."
        )

    ns = "http://www.opengis.net/kml/2.2"
    ET.register_namespace("", ns)
    kml = ET.Element(f"{{{ns}}}kml")
    doc = ET.SubElement(kml, "Document")
    ET.SubElement(doc, "name").text = title

    for v in pinned:
        pm = ET.SubElement(doc, "Placemark")
        ET.SubElement(pm, "name").text = v.ref.name
        desc = (
            f"<b>{html_mod.escape(v.ref.category or '')}</b><br/>"
            f"{html_mod.escape(v.ref.address or '')}<br/>"
            f"{html_mod.escape(v.ref.city or '')}<br/><br/>"
            f"Total check-ins: {v.total}<br/>"
            f"Unique: {v.unique}<br/>"
            f"Monthly: {v.monthly}<br/>"
            f"You: {v.you if v.you is not None else '-'}<br/>"
            # An empty href would render as a link back to the current page.
            + (f'<a href="{v.ref.url}">View on Untappd</a>' if v.ref.url else "")
        )
        ET.SubElement(pm, "description").text = desc

        # ExtendedData gives My Maps real, styleable columns.
        ext = ET.SubElement(pm, "ExtendedData")
        row = v.to_row()
        for key in ("category", "address", "city", "total", "unique",
                    "monthly", "you", "url"):
            data = ET.SubElement(ext, "Data", {"name": key})
            ET.SubElement(data, "value").text = str(row[key])

        point = ET.SubElement(pm, "Point")
        # KML is lon,lat -- the single most common bug in hand-rolled KML.
        ET.SubElement(point, "coordinates").text = f"{v.lng:.6f},{v.lat:.6f},0"

    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(kml).write(path, encoding="utf-8", xml_declaration=True)
    log.info("Wrote %d placemarks -> %s", len(pinned), path)
    return path


def _located(venues: list[Venue], fmt: str) -> list[Venue]:
    """Drop venues with no coordinates, loudly. Same rule as the KML: a pin in
    the wrong place is worse than a venue that simply is not on the map."""
    pinned = [v for v in venues if v.has_coords]
    skipped = len(venues) - len(pinned)
    if skipped:
        log.warning("%d venue(s) have no coordinates and are not in the %s.",
                    skipped, fmt)
    return pinned


def _stats_line(v: Venue) -> str:
    bits = [f"{v.total:,} check-ins" if v.total is not None else "check-ins n/a"]
    if v.unique is not None:
        bits.append(f"{v.unique:,} unique")
    if v.monthly is not None:
        bits.append(f"{v.monthly:,}/month")
    if v.ref.category:
        bits.append(v.ref.category)
    return " | ".join(bits)


def write_geojson(venues: list[Venue], path: Path) -> Path:
    """GeoJSON, for anything that is not Google.

    Organic Maps, OsmAnd and every OSM-based client import this as bookmarks
    that render on the everyday map -- which is what the project is actually
    for, and what the My Maps layer gives up.
    """
    features = []
    for v in _located(venues, "GeoJSON"):
        row = v.to_row()
        features.append({
            "type": "Feature",
            # GeoJSON is [longitude, latitude]. The same trap as the KML, and
            # it fails silently: the pins land, just in another hemisphere.
            "geometry": {"type": "Point", "coordinates": [v.lng, v.lat]},
            "properties": {
                "name": v.ref.name,
                "description": _stats_line(v),
                **{k: row[k] for k in ("category", "address", "city", "url")},
                "total": v.total, "unique": v.unique,
                "monthly": v.monthly, "you": v.you,
            },
        })

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, indent=1),
        encoding="utf-8",
    )
    log.info("Wrote %d features -> %s", len(features), path)
    return path


def write_gpx(venues: list[Venue], path: Path, title: str) -> Path:
    """GPX waypoints. The lowest common denominator every map app reads."""
    ns = "http://www.topografix.com/GPX/1/1"
    ET.register_namespace("", ns)
    gpx = ET.Element(f"{{{ns}}}gpx", {
        "version": "1.1", "creator": "beer-in-this-town",
    })
    meta = ET.SubElement(gpx, "metadata")
    ET.SubElement(meta, "name").text = title

    located = _located(venues, "GPX")
    for v in located:
        # GPX puts coordinates in attributes, not a body -- and names them,
        # so this is the one format where the lat/lon order cannot be got
        # wrong by accident.
        wpt = ET.SubElement(gpx, "wpt", {"lat": f"{v.lat:.6f}", "lon": f"{v.lng:.6f}"})
        ET.SubElement(wpt, "name").text = v.ref.name
        ET.SubElement(wpt, "desc").text = _stats_line(v)
        if v.ref.url:
            ET.SubElement(wpt, "link", {"href": v.ref.url})

    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(gpx).write(path, encoding="utf-8", xml_declaration=True)
    log.info("Wrote %d waypoints -> %s", len(located), path)
    return path


def diff_against_previous(
    venues: list[Venue], query: str
) -> dict[str, list[dict]]:
    """Compare this run to the last one *for the same city*.

    Returns new / gone / changed buckets.
    """
    current = {v.ref.venue_id: v.to_row() for v in venues}
    previous = _read_baseline(query)

    new_ids = current.keys() - previous.keys()
    gone_ids = previous.keys() - current.keys()

    changed = []
    for vid in current.keys() & previous.keys():
        before, after = previous[vid], current[vid]
        deltas = {
            k: (before.get(k), after.get(k))
            for k in ("total", "unique", "monthly", "you")
            if str(before.get(k)) != str(after.get(k))
        }
        if deltas:
            changed.append({"venue_id": vid, "name": after["name"], "deltas": deltas})

    return {
        "new": [current[v] for v in sorted(new_ids)],
        "gone": [previous[v] for v in sorted(gone_ids)],
        "changed": changed,
    }


def commit_run(venues: list[Venue], query: str) -> None:
    """Persist this run as the baseline for the next diff of this city."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    baseline_path(query).write_text(
        json.dumps({v.ref.venue_id: v.to_row() for v in venues}, indent=1),
        encoding="utf-8",
    )
    # The unscoped file has now been superseded. Rename rather than delete:
    # it is the user's only copy of whatever ran before the upgrade.
    if LEGACY_PREVIOUS_RUN.exists():
        superseded = LEGACY_PREVIOUS_RUN.with_suffix(".superseded.json")
        LEGACY_PREVIOUS_RUN.replace(superseded)
        log.info("Baselines are now per-city; kept the old one as %s.",
                 superseded.name)


def write_diff_outputs(diff: dict[str, list[dict]], stamp: str) -> None:
    if diff["new"]:
        path = DATA_DIR / f"new_venues_{stamp}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(diff["new"])
        log.info("%d NEW venue(s) -> %s", len(diff["new"]), path)
    else:
        log.info("No new venues since the last run.")

    if diff["gone"]:
        log.info("%d venue(s) disappeared from results: %s",
                 len(diff["gone"]), ", ".join(g["name"] for g in diff["gone"][:10]))
    if diff["changed"]:
        log.info("%d venue(s) changed stats.", len(diff["changed"]))
    (STATE_DIR / f"diff_{stamp}.json").write_text(
        json.dumps(diff, indent=1), encoding="utf-8"
    )


def today_stamp() -> str:
    return date.today().isoformat()
