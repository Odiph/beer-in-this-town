"""Offline tests for output formats, the diff, and the agent contract."""
from __future__ import annotations

import json

import pytest

from untappd_maps.agent_io import Envelope, Problem, fail
from untappd_maps.export import diff_against_previous, write_csv, write_kml
from untappd_maps.models import Venue, VenueRef
from untappd_maps.pin_to_list import _search_url, places_from_csv


@pytest.fixture
def venue() -> Venue:
    ref = VenueRef(
        venue_id="7480946",
        slug="american-taproom-waterloo",
        name="American Taproom - Waterloo",
        category="Beer Bar",
        address="261 Waterloo St, #01-23",
        city="Singapore, Singapore",
    )
    return Venue(ref=ref, total=20259, unique=2451, monthly=136, you=0,
                 lat=1.2982997, lng=103.8520951, geo_source="embedded")


# --- KML ------------------------------------------------------------------
@pytest.mark.unit
def test_kml_uses_lon_lat_order(tmp_path, venue):
    """KML is lon,lat. Getting this backwards is the classic silent map bug."""
    text = write_kml([venue], tmp_path / "t.kml", "Singapore Bars").read_text("utf-8")
    assert "103.852095,1.298300,0" in text


@pytest.mark.unit
def test_kml_carries_name_and_stats(tmp_path, venue):
    text = write_kml([venue], tmp_path / "t.kml", "Singapore Bars").read_text("utf-8")
    assert "<name>American Taproom - Waterloo</name>" in text
    assert "20259" in text
    assert 'Data name="total"' in text


@pytest.mark.unit
def test_kml_skips_venues_without_coordinates(tmp_path, venue):
    import dataclasses

    no_coords = dataclasses.replace(venue, lat=None, lng=None)
    text = write_kml([venue, no_coords], tmp_path / "t.kml", "X").read_text("utf-8")
    assert text.count("<Placemark>") == 1


@pytest.mark.unit
def test_kml_refuses_to_exceed_my_maps_layer_cap(tmp_path, venue):
    """My Maps truncates >2000 rows silently, so refuse to generate one."""
    with pytest.raises(ValueError, match="2000"):
        write_kml([venue] * 2001, tmp_path / "t.kml", "X")


# --- CSV ------------------------------------------------------------------
@pytest.mark.unit
def test_csv_roundtrip(tmp_path, venue):
    path = write_csv([venue], tmp_path / "t.csv")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].startswith("venue_id,name,category")
    assert "20259" in lines[1]


# --- diffing --------------------------------------------------------------
@pytest.mark.unit
def test_first_run_reports_everything_as_new(tmp_path, venue, monkeypatch):
    monkeypatch.setattr("untappd_maps.export.PREVIOUS_RUN", tmp_path / "none.json")
    diff = diff_against_previous([venue])
    assert len(diff["new"]) == 1
    assert diff["gone"] == []


@pytest.mark.unit
def test_changed_stats_are_detected(tmp_path, venue, monkeypatch):
    import dataclasses

    baseline = tmp_path / "prev.json"
    baseline.write_text(json.dumps({venue.ref.venue_id: venue.to_row()}), "utf-8")
    monkeypatch.setattr("untappd_maps.export.PREVIOUS_RUN", baseline)

    diff = diff_against_previous([dataclasses.replace(venue, total=20300)])
    assert diff["new"] == []
    # Values keep their JSON types; only the comparison is stringified.
    assert diff["changed"][0]["deltas"]["total"] == (20259, 20300)


# --- search URLs ----------------------------------------------------------
@pytest.mark.unit
def test_region_is_appended(venue):
    assert "Singapore" in _search_url("Bar X", "1 Foo St", "Singapore")


@pytest.mark.unit
def test_region_is_not_duplicated():
    url = _search_url("Bar X", "1 Foo St, Singapore", "Singapore")
    assert url.count("Singapore") == 1


@pytest.mark.unit
def test_region_can_be_disabled():
    assert "Singapore" not in _search_url("Bar X", "1 Foo St", None)


@pytest.mark.unit
def test_place_without_address_still_builds_a_url():
    assert "Bar+X" in _search_url("Bar X", None, None)


@pytest.mark.unit
def test_places_from_csv_reads_name_and_address(tmp_path, venue):
    path = write_csv([venue], tmp_path / "t.csv")
    places = places_from_csv(path)
    assert places == [("American Taproom - Waterloo",
                       "261 Waterloo St, #01-23, Singapore, Singapore")]


# --- the agent contract ---------------------------------------------------
@pytest.mark.unit
def test_envelope_is_valid_json_with_stable_keys():
    env = Envelope(command="run", ok=True, data={"venues": 100})
    payload = json.loads(env.to_json())
    assert payload["command"] == "run"
    assert payload["ok"] is True
    assert payload["schema_version"] == "1.0"
    assert "error" not in payload  # omitted when there is none


@pytest.mark.unit
def test_failure_envelope_carries_a_machine_readable_remedy():
    env = fail("pin", Problem(code="not_signed_in", message="nope",
                              remedy="python -m untappd_maps bootstrap"))
    payload = json.loads(env.to_json())
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_signed_in"
    # A remedy that is a runnable command is surfaced as a next action.
    assert payload["next_actions"] == ["python -m untappd_maps bootstrap"]
