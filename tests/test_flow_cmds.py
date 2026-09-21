"""`enrich`, `filter` and `export`: the steps after the sweep.

Each reads the previous step's file for a city and writes the next. Every
path here is offline: search and fetch are injected, and `data/` is the
sandbox's.
"""
from __future__ import annotations

import argparse
import csv
import json
from xml.etree import ElementTree as ET

import pytest

from beer_in_this_town import flow_cmds
from beer_in_this_town.config import Settings, city_slug, stage_path
from beer_in_this_town.export import write_csv
from beer_in_this_town.flow_cmds import (
    ENRICHED_CSV,
    EXCLUDED_CSV,
    SWEEP_CSV,
    VENUES_CSV,
    add_parsers,
    cmd_enrich,
    cmd_export,
    cmd_filter,
    dispatch,
)
from beer_in_this_town.models import Venue, VenueRef
from beer_in_this_town.notes import notes_from_csv
from beer_in_this_town.overpass import ATTRIBUTION
from beer_in_this_town.pin_to_list import places_from_csv

pytestmark = pytest.mark.unit

CITY = "Tel Aviv"
S = Settings()


def swept(name, lat=32.0700, lng=34.7846):
    return Venue(ref=VenueRef(venue_id="", slug="", name=name, category=None,
                              address=None, city=CITY),
                 total=None, unique=None, monthly=None, you=None,
                 lat=lat, lng=lng, geo_source="app" if lat else "none")


def page(vid, name, category, lat=32.0701, lng=34.7847, total=900,
         unique=400, monthly=12):
    ref = VenueRef(venue_id=vid, slug=name.lower().replace(" ", "-"),
                   name=name, category=category, address="10 HaArba'a St",
                   city="Tel Aviv, ישראל")
    return Venue(ref=ref, total=total, unique=unique, monthly=monthly,
                 you=None, lat=lat, lng=lng, geo_source="embedded")


def write_sweep(venues):
    return write_csv(venues, stage_path(CITY, SWEEP_CSV))


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


class Web:
    def __init__(self, pages):
        self.pages = {p.ref.venue_id: p for p in pages}
        self.searched: list[str] = []

    def search(self, q):
        self.searched.append(q)
        return [p.ref for p in self.pages.values()
                if p.ref.name.casefold() in q.casefold()]

    def fetch(self, ref):
        return self.pages[ref.venue_id]


def run_all(pages, sweep):
    write_sweep(sweep)
    web = Web(pages)
    env = cmd_enrich(S, CITY, search=web.search, fetch=web.fetch)
    assert env.ok, env
    env = cmd_filter(S, CITY)
    assert env.ok, env
    return env


# --- paths and parser -----------------------------------------------------

def test_city_slug_and_stage_path():
    assert city_slug("Tel Aviv") == "tel-aviv"
    assert city_slug("../../etc") == "etc"
    assert city_slug("תל אביב") == "תל-אביב"
    assert stage_path("Tel Aviv", "1_sweep.csv").parts[-2:] == (
        "tel-aviv", "1_sweep.csv")


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true")
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", parents=[common])
    add_parsers(sub, common)
    return p


def test_parsers_register_the_three_commands():
    p = parser()
    a = p.parse_args(["enrich", "--city", CITY, "--json"])
    assert (a.cmd, a.city, a.in_path) == ("enrich", CITY, None)
    a = p.parse_args(["filter", "--city", CITY, "--in", "x.csv"])
    assert a.in_path == "x.csv"
    a = p.parse_args(["export", "--city", CITY])
    assert a.format == "kml,gpx,geojson"


def test_dispatch_ignores_other_commands():
    assert dispatch(parser().parse_args(["status"]), S) is None


def test_dispatch_without_a_city_refuses():
    env = dispatch(parser().parse_args(["filter"]), S)
    assert not env.ok and env.error.code == "no_city"


def test_dispatch_routes_to_the_command():
    env = dispatch(parser().parse_args(["export", "--city", CITY]), S)
    assert env.command == "export"
    assert env.error.code == "stage_input_missing"


# --- missing inputs name the command that makes them ----------------------

@pytest.mark.parametrize("fn,producer", [
    (lambda: cmd_enrich(S, CITY, search=lambda q: [], fetch=None), "sweep"),
    (lambda: cmd_filter(S, CITY), "enrich"),
    (lambda: cmd_export(S, CITY), "filter"),
])
def test_a_missing_input_is_stage_input_missing(fn, producer):
    env = fn()
    assert not env.ok
    assert env.error.code == "stage_input_missing"
    want = f'python -m beer_in_this_town {producer} --city "{CITY}" --json'
    assert env.error.remedy == want
    assert env.next_actions == [want]


def test_in_overrides_the_input(tmp_path):
    src = write_csv([swept("Lauter")], tmp_path / "mine.csv")
    web = Web([page("1", "Lauter", "Bar")])
    env = cmd_enrich(S, CITY, str(src), search=web.search, fetch=web.fetch)
    assert env.ok and env.data["input"] == str(src)
    assert stage_path(CITY, ENRICHED_CSV).exists()


# --- enrich ----------------------------------------------------------------

def test_enrich_joins_and_keeps_the_unresolved():
    write_sweep([swept("Lauter"), swept("Nowhere Bar"), swept("Unplaced", None)])
    web = Web([page("1", "Lauter", "Bar, Beer Store")])
    env = cmd_enrich(S, CITY, search=web.search, fetch=web.fetch)
    assert env.ok
    assert env.data["venues"] == 3 and env.data["resolved"] == 1
    assert env.data["statuses"] == {"resolved": 1, "no_match": 1,
                                    "unlocated": 1}
    assert env.next_actions == [
        f'python -m beer_in_this_town filter --city "{CITY}" --json']
    out = rows(stage_path(CITY, ENRICHED_CSV))
    assert [r["resolution"] for r in out] == ["resolved", "no_match",
                                              "unlocated"]
    assert out[0]["total"] == "900" and out[0]["venue_id"] == "1"
    # Unknown stays unknown: blank, never 0, and no invented id.
    assert out[1]["total"] == "" and out[1]["venue_id"] == ""
    assert out[1]["sweep_name"] == "Nowhere Bar"
    assert any("unknown counts" in w for w in env.warnings)


def test_enrich_of_an_empty_sweep_writes_an_empty_stage():
    write_sweep([])
    env = cmd_enrich(S, CITY, search=lambda q: [], fetch=lambda r: None)
    assert env.ok and env.data["venues"] == 0
    assert rows(stage_path(CITY, ENRICHED_CSV)) == []


def test_enrich_without_pins_opens_no_browser(monkeypatch):
    """Live path, but nothing can be matched: it must not open Chrome."""
    import beer_in_this_town.resolve as resolve

    def boom(*a, **k):
        raise AssertionError("opened a browser")

    monkeypatch.setattr(resolve, "BrowserNameSearch", boom)
    write_sweep([swept("Unplaced", None)])
    env = cmd_enrich(S, CITY)
    assert env.ok and env.data["statuses"] == {"unlocated": 1}


def test_enrich_refuses_when_robots_disallows(monkeypatch):
    import beer_in_this_town.http_client as http_client

    class Client:
        def __init__(self, s):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def robots_disallows_scraping(self):
            return True

    monkeypatch.setattr(http_client, "PoliteClient", Client)
    write_sweep([swept("Lauter")])
    env = cmd_enrich(S, CITY)
    assert not env.ok and env.error.code == "robots_disallow"
    assert env.next_actions == []
    assert not stage_path(CITY, ENRICHED_CSV).exists()


# --- filter ----------------------------------------------------------------

def test_filter_splits_with_reasons_and_flags():
    pages = [
        page("1", "Lauter", "Bar, Beer Store"),
        page("2", "Vino Rosso", "Wine Bar"),
        page("3", "Quiet Pub", "Pub", total=500, monthly=0),
        page("4", "My Flat", "Bar", total=300, unique=2),
        page("5", "Shufersal", "Supermarket"),
    ]
    env = run_all(pages, [swept(p.ref.name) for p in pages]
                  + [swept("Nowhere Bar")])
    assert env.data["kept"] == 3 and env.data["excluded"] == 3
    kept = rows(stage_path(CITY, VENUES_CSV))
    gone = rows(stage_path(CITY, EXCLUDED_CSV))
    assert [r["name"] for r in kept] == ["Lauter", "Quiet Pub", "Nowhere Bar"]
    assert {r["name"]: r["reason"] for r in gone} == {
        "Vino Rosso": "wine bar",
        "My Flat": "private: 2 unique over 300 check-ins",
        "Shufersal": "supermarket",
    }
    quiet = next(r for r in kept if r["name"] == "Quiet Pub")
    assert quiet["flag"] == "possibly_closed"
    assert kept[-1]["kind"] == "uncategorised"
    assert env.data["flagged"] == {"possibly_closed": 1}
    assert env.next_actions == [
        f'python -m beer_in_this_town export --city "{CITY}" --json']
    assert all(" pin " not in a and " closures " not in a
               for a in env.next_actions)


def test_three_venues_feeds_pin_and_notes_directly():
    run_all([page("1", "Lauter", "Bar")], [swept("Lauter"), swept("Nowhere")])
    path = stage_path(CITY, VENUES_CSV)
    places = places_from_csv(path)
    assert places[0] == ("Lauter", "10 HaArba'a St, Tel Aviv, ישראל")
    assert places[1] == ("Nowhere", CITY)
    notes = notes_from_csv(path)
    # Only the joined venue has stats worth a note; unknown is not zero.
    assert [n[0] for n in notes] == ["Lauter"]
    assert "900 check-ins" in notes[0][2]


# --- export ----------------------------------------------------------------

def test_export_writes_all_three_with_osm_credit():
    run_all([page("1", "Lauter", "Bar")],
            [swept("Lauter"), swept("Nowhere Bar"), swept("Lost", None)])
    env = cmd_export(S, CITY)
    assert env.ok
    assert set(env.data["files"]) == {"kml", "gpx", "geojson"}
    assert env.data["placemarks"] == 2 and env.data["unplaced"] == 1
    assert env.data["attribution"] == ATTRIBUTION
    for fmt in ("kml", "gpx", "geojson"):
        assert ATTRIBUTION in stage_path(CITY, f"venues.{fmt}").read_text(
            encoding="utf-8")
    ET.parse(stage_path(CITY, "venues.kml"))
    ET.parse(stage_path(CITY, "venues.gpx"))
    gj = json.loads(stage_path(CITY, "venues.geojson").read_text("utf-8"))
    assert gj["attribution"] == ATTRIBUTION and len(gj["features"]) == 2
    assert env.next_actions == ["python -m beer_in_this_town status --json"]
    hints = " ".join(env.hints)
    assert "New list" in hints
    assert f'pin --csv "{stage_path(CITY, VENUES_CSV)}"' in hints
    assert "--limit 3" in hints


def test_export_without_osm_coordinates_carries_no_credit():
    run_all([page("1", "Lauter", "Bar")], [swept("Lauter")])
    env = cmd_export(S, CITY, formats="geojson")
    assert env.ok and env.data["attribution"] is None
    assert list(env.data["files"]) == ["geojson"]
    gj = json.loads(stage_path(CITY, "venues.geojson").read_text("utf-8"))
    assert "attribution" not in gj


def test_export_rejects_an_unknown_format():
    env = cmd_export(S, CITY, formats="kml,shp")
    assert not env.ok and env.error.code == "bad_format"


def test_parse_formats_dedupes_and_orders():
    assert flow_cmds.parse_formats("GPX, kml,gpx") == ("gpx", "kml")
    with pytest.raises(ValueError):
        flow_cmds.parse_formats(" , ")
