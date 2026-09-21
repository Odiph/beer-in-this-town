"""Turning a map sweep into the rows every exporter already understands.

A swept venue knows its name and where it is, and nothing else: no Untappd
id, no category, no check-in counts. Those come from the venue page, which is
a separate step. What matters here is that the gaps travel as gaps -- a
missing count must never reach a map popup as `None`, and never as `0`.
"""
from __future__ import annotations

from beer_in_this_town.app_export import to_venues
from beer_in_this_town.app_sweep import SweepResult
from beer_in_this_town.app_sweep import Venue as SweptVenue
from beer_in_this_town.export import write_csv, write_kml


def _result(*venues):
    return SweepResult(venues=list(venues))


def test_a_swept_venue_keeps_its_name_and_position():
    out = to_venues(_result(SweptVenue("Lauter", 1, 2, 32.07, 34.78)), "Tel Aviv")
    assert out[0].ref.name == "Lauter"
    assert (out[0].lat, out[0].lng) == (32.07, 34.78)


def test_the_position_says_where_it_came_from():
    """`geo_source` is how a reader tells a map-pin fit (~30 m) from a
    venue page's own coordinates."""
    out = to_venues(_result(SweptVenue("Lauter", 1, 2, 32.07, 34.78)), "Tel Aviv")
    assert out[0].geo_source == "app"


def test_stats_are_unknown_not_zero():
    out = to_venues(_result(SweptVenue("Lauter", 1, 2, 32.07, 34.78)), "Tel Aviv")
    v = out[0]
    assert (v.total, v.unique, v.monthly, v.you) == (None, None, None, None)
    assert v.ref.url == "", "no id, so no link -- not a guessed one"


def test_a_venue_without_coordinates_stays_unlocated():
    out = to_venues(_result(SweptVenue("Lauter", 1, 2)), "Tel Aviv")
    assert not out[0].has_coords


def test_the_map_popup_says_unknown_rather_than_none(tmp_path):
    out = to_venues(_result(SweptVenue("Lauter", 1, 2, 32.07, 34.78)), "Tel Aviv")
    kml = write_kml(out, tmp_path / "t.kml", "t").read_text(encoding="utf-8")
    assert "None" not in kml
    assert "n/a" in kml


def test_the_csv_round_trips(tmp_path):
    out = to_venues(_result(SweptVenue("Lauter", 1, 2, 32.07, 34.78)), "Tel Aviv")
    text = write_csv(out, tmp_path / "t.csv").read_text(encoding="utf-8-sig")
    assert "Lauter" in text and "app" in text
