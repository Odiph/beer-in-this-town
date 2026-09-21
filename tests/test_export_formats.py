"""GeoJSON and GPX, so the everyday-map use case has a path that isn't Google.

Organic Maps and OsmAnd import these as bookmarks that render on the everyday
map -- offline, no account, no terms prohibiting it. The KML/My Maps path gives
up the everyday-map pins; `pin` gets them back by automating a UI Google says
not to automate. This is the third option, and it is the only one that is both.
"""
from __future__ import annotations

import dataclasses
import json
from xml.etree import ElementTree as ET

import pytest

from beer_in_this_town.export import write_geojson, write_gpx
from beer_in_this_town.models import Venue, VenueRef

GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def _venue(name: str, lat: float | None, lng: float | None) -> Venue:
    v = Venue(
        ref=VenueRef(venue_id="1", slug="ghost-whale", name=name,
                     category="Beer Bar", address="24 Clapham High St",
                     city="London"),
        total=2628, unique=807, monthly=10, you=3,
    )
    return v.with_coords(lat, lng, "embedded") if lat is not None else v


@pytest.mark.unit
def test_geojson_puts_longitude_first(tmp_path):
    """GeoJSON is [lng, lat]. Swapping them lands London in Somalia."""
    path = write_geojson([_venue("Ghost Whale", 51.4626, -0.1385)],
                         tmp_path / "out.geojson")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["type"] == "FeatureCollection"
    assert doc["features"][0]["geometry"]["coordinates"] == [-0.1385, 51.4626]


@pytest.mark.unit
def test_geojson_carries_the_stats(tmp_path):
    """The numbers are the whole point; a bare pin is worth less than nothing."""
    path = write_geojson([_venue("Ghost Whale", 51.4626, -0.1385)],
                         tmp_path / "out.geojson")
    props = json.loads(path.read_text(encoding="utf-8"))["features"][0]["properties"]
    assert props["name"] == "Ghost Whale"
    assert props["total"] == 2628
    assert props["url"].endswith("/v/ghost-whale/1")


@pytest.mark.unit
def test_gpx_waypoints_carry_name_and_description(tmp_path):
    """OsmAnd and Organic Maps show <name> in the list and <desc> on tap."""
    path = write_gpx([_venue("Ghost Whale", 51.4626, -0.1385)],
                     tmp_path / "out.gpx", "London Bars")
    root = ET.parse(path).getroot()
    wpt = root.find("gpx:wpt", GPX_NS)
    assert wpt.get("lat") == "51.462600" and wpt.get("lon") == "-0.138500"
    assert wpt.find("gpx:name", GPX_NS).text == "Ghost Whale"
    assert "2,628" in wpt.find("gpx:desc", GPX_NS).text


@pytest.mark.unit
@pytest.mark.parametrize("writer,suffix", [(write_geojson, ".geojson"),
                                           (write_gpx, ".gpx")])
def test_a_venue_with_no_coordinates_is_not_invented(writer, suffix, tmp_path):
    """Same rule as the KML: no pin beats a pin in the wrong place."""
    venues = [_venue("Located", 51.4626, -0.1385), _venue("Unlocated", None, None)]
    args = (venues, tmp_path / f"out{suffix}")
    path = writer(*args, "London Bars") if writer is write_gpx else writer(*args)
    body = path.read_text(encoding="utf-8")
    assert "Located" in body and "Unlocated" not in body


@pytest.mark.unit
def test_gpx_declares_the_namespace_importers_look_for(tmp_path):
    """A GPX without the 1.1 namespace is rejected by every reader worth having."""
    path = write_gpx([_venue("Ghost Whale", 51.4626, -0.1385)],
                     tmp_path / "out.gpx", "London Bars")
    assert 'http://www.topografix.com/GPX/1/1' in path.read_text(encoding="utf-8")
    assert ET.parse(path).getroot().get("version") == "1.1"


# --- the --format boundary -------------------------------------------------
@pytest.mark.unit
def test_format_defaults_keep_existing_behaviour():
    """Adding formats must not change what an existing user gets."""
    from beer_in_this_town.cli import parse_formats
    assert parse_formats("kml") == ("kml",)


@pytest.mark.unit
def test_formats_are_validated_at_the_boundary():
    """A typo must fail immediately, not write no map and report success."""
    from beer_in_this_town.cli import parse_formats
    assert parse_formats("gpx, geojson") == ("gpx", "geojson")
    with pytest.raises(ValueError, match="kmz"):
        parse_formats("kmz")
    with pytest.raises(ValueError):
        parse_formats("  ")


@pytest.mark.unit
@pytest.mark.parametrize("writer,suffix", [(write_geojson, ".geojson"),
                                           (write_gpx, ".gpx")])
def test_a_venue_with_no_page_carries_no_empty_link(writer, suffix, tmp_path):
    """An empty href renders as a link back to the page you are already on."""
    v = _venue("Ghost Whale", 51.4626, -0.1385)
    stripped = dataclasses.replace(
        v, ref=dataclasses.replace(v.ref, slug="", venue_id="Ghost Whale"))
    args = ([stripped], tmp_path / f"out{suffix}")
    path = writer(*args, "London Bars") if writer is write_gpx else writer(*args)
    body = path.read_text(encoding="utf-8")
    assert "Ghost Whale" in body
    assert 'href=""' not in body
