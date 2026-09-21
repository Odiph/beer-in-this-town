"""Offline tests for output formats, the diff, and the agent contract."""
from __future__ import annotations

import json

import pytest

from beer_in_this_town.agent_io import Envelope, Problem, fail
from beer_in_this_town.export import diff_against_previous, write_csv, write_kml
from beer_in_this_town.models import Venue, VenueRef
from beer_in_this_town.pin_to_list import _search_url, places_from_csv


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
    monkeypatch.setattr("beer_in_this_town.export.STATE_DIR", tmp_path)
    monkeypatch.setattr("beer_in_this_town.export.LEGACY_PREVIOUS_RUN",
                        tmp_path / "none.json")
    diff = diff_against_previous([venue], query="singapore")
    assert len(diff["new"]) == 1
    assert diff["gone"] == []


@pytest.mark.unit
def test_changed_stats_are_detected(tmp_path, venue, monkeypatch):
    import dataclasses

    monkeypatch.setattr("beer_in_this_town.export.STATE_DIR", tmp_path)
    monkeypatch.setattr("beer_in_this_town.export.LEGACY_PREVIOUS_RUN",
                        tmp_path / "none.json")
    baseline = tmp_path / "previous_run_singapore.json"
    baseline.write_text(json.dumps({venue.ref.venue_id: venue.to_row()}), "utf-8")

    diff = diff_against_previous(
        [dataclasses.replace(venue, total=20300)], query="singapore")
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
                              remedy="python -m beer_in_this_town bootstrap"))
    payload = json.loads(env.to_json())
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_signed_in"
    # A remedy that is a runnable command is surfaced as a next action.
    # bootstrap opens a browser and waits for a person, so it is a hint --
    # not a next action. This test used to assert the opposite, which is how
    # `fail()` kept putting it back after `status` had stopped offering it.
    assert payload["next_actions"] == []
    assert payload["hints"] == ["python -m beer_in_this_town bootstrap"]


# --- place identity and journal keys (review findings M2, M3) -------------
@pytest.mark.unit
@pytest.mark.parametrize(
    ("requested", "heading"),
    [
        ("Brewerkz", "Brewerkz Riverside Point"),          # Google adds words
        ("SG TAPS", "SG Taps Restaurant"),                 # case + suffix
        ("Welcome Ren Min", "Welcome Ren Min - Craft Brewery Taproom"),
    ],
)
def test_place_matches_accepts_google_renamings(requested, heading):
    from beer_in_this_town.pin_to_list import place_matches

    assert place_matches(requested, heading)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("requested", "heading"),
    [
        ("Malt Craft Beer Bar", "Starbucks Orchard Road"),
        ("Druggists", "Boots Pharmacy"),
        ("Good Luck", "Marina Bay Sands"),
        # Only 1 of 3 words overlap. This may be a different branch of the
        # same chain, so skipping for manual review beats guessing.
        ("TAP - 9 Penang", "TAP Craft Beer Bar"),
    ],
)
def test_place_matches_rejects_a_different_venue(requested, heading):
    from beer_in_this_town.pin_to_list import place_matches

    assert not place_matches(requested, heading)


@pytest.mark.unit
def test_journal_key_separates_same_named_outlets():
    from beer_in_this_town.pin_to_list import journal_key

    a = journal_key("Harry's", "1 Boat Quay, Singapore")
    b = journal_key("Harry's", "9 Orchard Road, Singapore")
    assert a != b


@pytest.mark.unit
def test_journal_lookup_honours_pre_migration_keys():
    """A journal written before keys included the address must still count."""
    from beer_in_this_town.pin_to_list import _lookup

    old_style = {"Malthouse": "ok"}
    assert _lookup(old_style, "Malthouse", "685 East Coast Rd") == "ok"


@pytest.mark.unit
def test_retry_after_accepts_both_legal_forms():
    from beer_in_this_town.http_client import _retry_after_seconds

    assert _retry_after_seconds("120", 60) == 120
    # HTTP-date form used to raise ValueError and kill the run
    assert _retry_after_seconds("Wed, 21 Aug 2030 07:28:00 GMT", 60) > 0
    assert _retry_after_seconds("nonsense", 60) == 60
    assert _retry_after_seconds(None, 60) == 60


# --- the contract must not steer an agent into a write --------------------
@pytest.mark.unit
def test_next_actions_never_carries_a_write_or_a_comment(tmp_path, monkeypatch):
    """AGENTS.md says run the first action and repeat until the list is empty.

    It also says never run `pin` unasked. While `pin` was listed, those two
    rules contradicted each other and the loop won -- `pin` was the only entry
    that ever emptied the list, so an obedient agent was walked into the
    account write by the contract itself.
    """
    from beer_in_this_town import state
    from beer_in_this_town.config import Settings, stage_path

    for name in ("1_sweep.csv", "2_enriched.csv", "3_venues.csv"):
        path = stage_path("london", name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("venue_id\n1\n", encoding="utf-8")
    stage_path("london", "venues.kml").write_text("<kml/>", encoding="utf-8")
    session = tmp_path / "storage_state.json"
    session.write_text("{}", encoding="utf-8")
    s = Settings(storage_state=session)

    # A session file alone is not a working account, so the first action is
    # to prove it.
    assert state.next_actions(state.inspect_state(s), s) == [
        "python -m beer_in_this_town verify --json"
    ]

    state.record_verification({"google": {"ok": True}, "untappd": {"ok": True}},
                              ok=True)
    # There is no default city any more, so one has to be chosen before a
    # stage is on offer at all.
    state.record_intent("london")

    inspected = state.inspect_state(s)
    actions = state.next_actions(inspected, s)

    assert actions == [], "with the files in hand the loop must be able to end"

    # And on a state that DOES produce actions -- the previous version looped
    # over the empty list above, so the forbidden-word check never ran.
    stage_path("london", "venues.kml").unlink()
    pending = state.next_actions(state.inspect_state(s), s)
    assert pending, "a missing map file must still give the agent something to do"
    for command in pending:
        assert not command.lstrip().startswith("#")
        for forbidden in (" pin ", " notes ", "bootstrap"):
            assert forbidden not in f" {command} "


@pytest.mark.unit
def test_hints_are_prose_and_stay_out_of_next_actions():
    """A hint is for a person; nothing is told to execute it."""
    from beer_in_this_town.agent_io import Envelope

    env = Envelope(command="run", ok=True, hints=["a human can run: pin ..."])
    payload = json.loads(env.to_json())
    assert payload["hints"] == ["a human can run: pin ..."]
    assert payload["next_actions"] == []


@pytest.mark.unit
@pytest.mark.parametrize("remedy,promoted", [
    ("python -m beer_in_this_town run --no-upload --json", True),
    ("python -m beer_in_this_town status --json", True),
    ("python -m beer_in_this_town bootstrap", False),
    ('python -m beer_in_this_town pin --csv "x" --json', False),
    ('python -m beer_in_this_town notes --csv "x" --json', False),
    ("python -m beer_in_this_town run --json", False),   # uploads by default
    ("Create the list by hand in Google Maps.", False),
])
def test_only_safe_remedies_become_next_actions(remedy, promoted):
    """`fail()` promoted anything starting with "python".

    That put bootstrap, pin and notes straight back into next_actions after
    they had been taken out of `status` -- the same contradiction, through a
    different door. A bare `run` counts as unsafe because it uploads unless
    told not to.
    """
    from beer_in_this_town.agent_io import Problem, fail

    env = fail("x", Problem(code="c", message="m", remedy=remedy))
    payload = json.loads(env.to_json())
    assert bool(payload["next_actions"]) is promoted
    if not promoted:
        assert payload["hints"] == [remedy], "it must still reach the caller"
