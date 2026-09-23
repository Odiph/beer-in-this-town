"""Enrich a search sweep: the id is already known, so no name is guessed.

A map sweep row is a name and a pin, and enrich has to find which venue
page it is (London: 76 of 611 came back too_far on namesakes). A search
sweep row IS a venue page, so enrich fetches it by id. The position comes
from the page, and a page outside the city is dropped: "london" also finds
New London, CT, and London, Ontario.

No network.
"""
from __future__ import annotations

import json

import pytest

from beer_in_this_town import city_names
from beer_in_this_town.city_names import CityNames
from beer_in_this_town.config import Settings, stage_path
from beer_in_this_town.export import write_csv
from beer_in_this_town.flow_cmds import ENRICHED_CSV, SWEEP_CSV, cmd_enrich
from beer_in_this_town.models import Venue, VenueRef
from beer_in_this_town.resolve import enrich_rows

pytestmark = pytest.mark.unit

CITY = "London"
BOX = (51.28, 51.70, -0.51, 0.34)
PLACES = {"1": ("The Rake", 51.5051, -0.0906),
          "2": ("Tox Brewing", 41.3557, -72.0995),     # New London, CT
          "3": ("Kernel Taproom", None, None)}          # page without a map


def _searched(vid: str) -> Venue:
    name = PLACES[vid][0]
    return Venue(ref=VenueRef(venue_id=vid, slug=name.lower().replace(" ", "-"),
                              name=name, category="Pub", address=None,
                              city="London, Greater London, United Kingdom"),
                 total=None, unique=None, monthly=None, you=None)


def _page(ref: VenueRef) -> Venue:
    _, lat, lng = PLACES[ref.venue_id]
    return Venue(ref=ref, total=500, unique=300, monthly=20, you=None,
                 lat=lat, lng=lng, geo_source="embedded" if lat else "none")


def no_search(q):
    raise AssertionError(f"searched for {q!r}: a known id needs no search")


def test_a_known_id_is_fetched_directly_and_placed_from_its_page():
    fetched = []

    def fetch(ref):
        fetched.append(ref.url)
        return _page(ref)

    rows, report = enrich_rows([_searched("1")], CITY, no_search, fetch,
                               within=CityNames(CITY, ("london",), BOX).contains)
    assert fetched == ["https://untappd.com/v/the-rake/1"]
    (venue, res), = rows
    assert res.status == "resolved" and venue.total == 500
    assert (venue.lat, venue.lng) == (51.5051, -0.0906)


def test_a_page_outside_the_city_is_dropped_and_counted():
    rows, report = enrich_rows([_searched("1"), _searched("2")], CITY,
                               no_search, _page,
                               within=CityNames(CITY, ("london",), BOX).contains)
    assert [v.ref.venue_id for v, _ in rows] == ["1"]
    assert report.counts() == {"resolved": 1, "outside": 1}
    assert "41.36" in report.resolutions[1].detail


def test_a_page_with_no_position_is_kept_with_its_stats():
    (venue, res), = enrich_rows([_searched("3")], CITY, no_search, _page)[0]
    assert res.status == "resolved" and venue.total == 500
    assert venue.lat is None and "no coordinates" in res.detail


def test_unknown_bounds_keep_every_page():
    rows, _ = enrich_rows([_searched("2")], CITY, no_search, _page)
    assert len(rows) == 1


def test_the_command_enriches_a_search_sweep_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(city_names, "GAZETTEER_DIR", tmp_path)
    (tmp_path / "london.json").write_text(json.dumps({
        "city": CITY, "variants": ["london"], "bbox": list(BOX)}),
        encoding="utf-8")
    write_csv([_searched(v) for v in ("1", "2", "3")],
              stage_path(CITY, SWEEP_CSV))
    env = cmd_enrich(Settings(), CITY, search=no_search, fetch=_page)
    assert env.ok, env.error
    assert env.data["statuses"] == {"resolved": 2, "outside": 1}
    assert env.data["rows"] == 2
    assert stage_path(CITY, ENRICHED_CSV).exists()
