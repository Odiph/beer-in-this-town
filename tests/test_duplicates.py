"""#9: the same bar under several Untappd ids, flagged -- never merged.

Measured 2026-09-26 before building this: in the London and Tel Aviv map
sweeps no two venues share a normalised name within 300 m (London's five
same-name pairs are separate branches 6-13 km apart), and no two cached
venue pages link the same Foursquare place. Duplicates come with the web
search, which returns every Untappd entry for a name ("Lager & Ale" twice
in Tel Aviv). So: a flag on the quieter entry pointing at the busier one,
visible in the CSV, and the decision left to a person. A wrong merge
deletes a real bar; a missed one costs a duplicate pin.
"""
from __future__ import annotations

import csv

import pytest

from beer_in_this_town.config import Settings
from beer_in_this_town.dedupe import name_key, possible_duplicates
from beer_in_this_town.flow_cmds import ENRICHED_FIELDS, cmd_filter

pytestmark = pytest.mark.unit


def row(vid, name, lat, lng, total, fsq=""):
    return {"venue_id": vid, "name": name, "lat": str(lat), "lng": str(lng),
            "total": str(total), "fsq_id": fsq}


@pytest.mark.parametrize("a, b", [
    ("Lager & Ale", "Lager & Ale"),
    ("The Wheatsheaf", "Wheatsheaf"),
    ("Cồn Market", "Con Market"),                     # accents folded
    ("Lager & Ale (לאגר אנד אייל)", "Lager & Ale"),   # bilingual suffix
    ("The Barrel Vault (Wetherspoon)", "Barrel Vault"),
])
def test_names_that_are_the_same_bar_share_a_key(a, b):
    assert name_key(a) == name_key(b)


def test_different_bars_keep_different_keys():
    assert name_key("Crown & Sceptre") != name_key("Rose & Crown")


def test_same_name_next_door_is_a_possible_duplicate_of_the_busier():
    rows = [row("1", "Lager & Ale", 32.0600, 34.7700, 1451),
            row("2", "Lager & Ale", 32.0601, 34.7701, 90)]
    assert possible_duplicates(rows) == {"2": "1"}


def test_same_name_across_town_is_two_branches_not_a_duplicate():
    # Ghost Whale Brixton and Putney: 7 km apart, both real.
    rows = [row("1", "Ghost Whale - Brixton", 51.4613, -0.1156, 88327),
            row("2", "Ghost Whale - Putney", 51.4637, -0.2170, 67641)]
    assert possible_duplicates(rows) == {}


def test_a_shared_foursquare_place_is_a_duplicate_whatever_the_names():
    rows = [row("1", "Old Name", 51.50, -0.10, 500, fsq="a" * 24),
            row("2", "New Name", 51.51, -0.11, 20, fsq="a" * 24)]
    assert possible_duplicates(rows) == {"2": "1"}


def test_rows_without_a_position_are_never_matched_by_name():
    rows = [row("1", "Lager & Ale", "", "", 1451),
            row("2", "Lager & Ale", "", "", 90)]
    assert possible_duplicates(rows) == {}


def test_a_chain_of_three_points_at_the_busiest():
    rows = [row("1", "Bar X", 51.5000, -0.1000, 10),
            row("2", "Bar X", 51.5001, -0.1001, 900),
            row("3", "Bar X", 51.5002, -0.1002, 50)]
    assert possible_duplicates(rows) == {"1": "2", "3": "2"}


def test_filter_flags_them_and_keeps_both(tmp_path):
    src = tmp_path / "2_enriched.csv"
    base = {k: "" for k in ENRICHED_FIELDS}
    rows = [
        {**base, "venue_id": "1", "name": "Lager & Ale", "category": "Beer Bar",
         "total": "1451", "unique": "600", "monthly": "20", "lat": "32.0600",
         "lng": "34.7700", "url": "https://untappd.com/v/lager-ale/1",
         "resolution": "resolved"},
        {**base, "venue_id": "2", "name": "Lager & Ale", "category": "Bar",
         "total": "90", "unique": "60", "monthly": "2", "lat": "32.0601",
         "lng": "34.7701", "url": "https://untappd.com/v/lager-ale-2/2",
         "resolution": "resolved"},
    ]
    with src.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ENRICHED_FIELDS)
        w.writeheader()
        w.writerows(rows)
    env = cmd_filter(Settings(), "Dup City", in_path=str(src))
    assert env.ok and env.data["kept"] == 2, "flagged, never dropped"
    assert env.data["flagged"].get("possible_duplicate") == 1
    with open(env.data["csv"], encoding="utf-8-sig") as fh:
        out = {r["venue_id"]: r for r in csv.DictReader(fh)}
    assert out["2"]["duplicate_of"] == "1" and out["1"]["duplicate_of"] == ""
    assert any("duplicate" in w for w in env.warnings)
