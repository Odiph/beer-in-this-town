"""CSV + KML output, and the run-to-run diff."""
from __future__ import annotations

import csv
import html as html_mod
import json
import logging
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from .config import DATA_DIR, STATE_DIR
from .models import CSV_FIELDS, Venue

log = logging.getLogger(__name__)

PREVIOUS_RUN = STATE_DIR / "previous_run.json"


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
            f'<a href="{v.ref.url}">View on Untappd</a>'
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


def diff_against_previous(venues: list[Venue]) -> dict[str, list[dict]]:
    """Compare this run to the last one. Returns new / gone / changed buckets."""
    current = {v.ref.venue_id: v.to_row() for v in venues}
    previous: dict[str, dict] = {}
    if PREVIOUS_RUN.exists():
        previous = json.loads(PREVIOUS_RUN.read_text(encoding="utf-8"))

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


def commit_run(venues: list[Venue]) -> None:
    """Persist this run as the baseline for the next diff."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PREVIOUS_RUN.write_text(
        json.dumps({v.ref.venue_id: v.to_row() for v in venues}, indent=1),
        encoding="utf-8",
    )


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
