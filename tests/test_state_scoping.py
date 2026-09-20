"""State artifacts belong to a city, not to the machine.

One global previous_run.json meant scraping London after Singapore diffed the
two against each other and then overwrote the Singapore history for good. One
global pinned.json meant a venue saved into one list was skipped when building
the next. And `status` reported the default city whatever you actually ran, so
the literal command it handed an agent could pin one city into another's list.
All offline.
"""
from __future__ import annotations

import json

import pytest

from beer_in_this_town import export, notes, pin_to_list, state
from beer_in_this_town.config import Settings, scope_slug
from beer_in_this_town.models import Venue, VenueRef


def _venue(vid: str, name: str, total: int = 10) -> Venue:
    return Venue(
        ref=VenueRef(venue_id=vid, slug=name.lower(), name=name,
                     category="Beer Bar", address=None, city=None),
        total=total, unique=total, monthly=total, you=None,
    )


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point every state-writing module at a scratch directory."""
    for mod in (export, state, pin_to_list, notes):
        monkeypatch.setattr(mod, "STATE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(export, "LEGACY_PREVIOUS_RUN",
                        tmp_path / "previous_run.json")
    monkeypatch.setattr(state, "LAST_RUN", tmp_path / "last_run.json")
    monkeypatch.setattr(state, "LEGACY_PINNED", tmp_path / "pinned.json")
    monkeypatch.setattr(pin_to_list, "LEGACY_JOURNAL", tmp_path / "pinned.json")
    monkeypatch.setattr(notes, "LEGACY_NOTE_JOURNAL", tmp_path / "noted.json")
    return tmp_path


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("singapore", "singapore"),
    ("Singapore Bars", "singapore-bars"),
    ("  London  ", "london"),
    ("Bars & Taprooms", "bars-taprooms"),
    ("../../etc/passwd", "etc-passwd"),
])
def test_scope_slug_is_filename_safe(raw, expected):
    """A city or list name becomes a filename, so it must not escape the dir."""
    assert scope_slug(raw) == expected


@pytest.mark.unit
def test_scope_slug_never_yields_an_empty_name():
    """A name of pure punctuation must still produce a usable file."""
    assert scope_slug("!!!") != ""
    assert "/" not in scope_slug("!!!") and "\\" not in scope_slug("!!!")


@pytest.mark.unit
def test_two_cities_keep_separate_baselines(state_dir):
    """The bug: London's run used to diff against Singapore's and erase it."""
    sg = [_venue("1", "Ghost Whale", total=100)]
    export.commit_run(sg, query="singapore")

    ldn = [_venue("2", "Kernel Taproom", total=50)]
    diff = export.diff_against_previous(ldn, query="london")
    # London has no history of its own: everything is new, nothing is "gone".
    assert len(diff["new"]) == 1
    assert diff["gone"] == []
    export.commit_run(ldn, query="london")

    # Singapore's baseline survived London entirely.
    again = export.diff_against_previous(sg, query="singapore")
    assert again["new"] == [] and again["gone"] == [] and again["changed"] == []


@pytest.mark.unit
def test_same_city_still_diffs(state_dir):
    """Scoping must not break the thing the baseline is for."""
    export.commit_run([_venue("1", "Ghost Whale", total=100)], query="singapore")
    diff = export.diff_against_previous(
        [_venue("1", "Ghost Whale", total=140)], query="singapore")
    assert diff["changed"][0]["deltas"]["total"] == (100, 140)


@pytest.mark.unit
def test_an_unscoped_baseline_is_adopted_not_discarded(state_dir):
    """Upgrading must not silently look like a brand-new city."""
    legacy = {"1": _venue("1", "Ghost Whale", total=100).to_row()}
    (state_dir / "previous_run.json").write_text(json.dumps(legacy),
                                                 encoding="utf-8")
    diff = export.diff_against_previous(
        [_venue("1", "Ghost Whale", total=140)], query="singapore")
    assert diff["new"] == [], "the legacy baseline should have been read"
    assert diff["changed"][0]["deltas"]["total"] == (100, 140)


@pytest.mark.unit
def test_pin_journals_are_per_list(state_dir):
    """A place saved into one list is not 'done' for the next one."""
    pin_to_list._save_journal({"Ghost Whale | None": "ok"}, "Singapore Bars")
    assert pin_to_list._load_journal("Singapore Breweries") == {}
    assert pin_to_list._load_journal("Singapore Bars") != {}


@pytest.mark.unit
def test_note_journals_are_per_list(state_dir):
    """Same for notes: a note written into one list says nothing about another."""
    notes._save_journal({"Ghost Whale | None": "ok"}, "Singapore Bars")
    assert notes._load_journal("Singapore Breweries") == {}


@pytest.mark.unit
def test_a_legacy_journal_is_adopted_by_exactly_one_list(state_dir):
    """Its list is unrecorded, so the guess is made once and never repeated.

    Letting a second list inherit "already saved" entries it never earned
    would skip real work and report success -- the silent under-delivery this
    project exists to refuse.
    """
    (state_dir / "pinned.json").write_text(
        json.dumps({"Ghost Whale | None": "ok"}), encoding="utf-8")

    assert pin_to_list._load_journal("Singapore Bars") == {"Ghost Whale | None": "ok"}
    # The adoption consumed the legacy file, so nothing else can inherit it.
    assert not (state_dir / "pinned.json").exists()
    assert pin_to_list._load_journal("Singapore Breweries") == {}


@pytest.mark.unit
def test_status_reports_the_city_that_actually_ran(state_dir, tmp_path, monkeypatch):
    """The dangerous bug: status defaulted to Singapore whatever you scraped."""
    data = tmp_path / "data"
    data.mkdir()
    csv = data / "venues_london_2026-09-20.csv"
    csv.write_text("venue_id\n", encoding="utf-8")
    monkeypatch.setattr(state, "DATA_DIR", data)
    state.record_run(query="london", map_title="London Bars", csv_path=csv)

    s = Settings()  # defaults are singapore / "Singapore Bars"
    inspected = state.inspect_state(s)
    assert inspected["last_run"]["query"] == "london"

    actions = " ".join(state.next_actions(inspected, s))
    assert "Singapore" not in actions, actions
    assert "London Bars" in actions


@pytest.mark.unit
def test_status_still_sees_a_pre_upgrade_pin_journal(state_dir, tmp_path, monkeypatch):
    """Reporting zero saved places to someone with dozens is its own lie.

    inspect_state is read-only, so it reads the unscoped journal rather than
    adopting it -- the one-shot adoption belongs to `pin`, which knows the list.
    """
    (tmp_path / "pinned.json").write_text(
        json.dumps({"Ghost Whale | None": "ok", "Smith Street | None": "failed"}),
        encoding="utf-8")
    monkeypatch.setattr(state, "DATA_DIR", tmp_path)

    inspected = state.inspect_state(Settings())
    assert inspected["pin_progress"]["ok"] == 1
    assert inspected["pin_progress"]["failed"] == 1
    assert inspected["pin_progress_scope"] == "unscoped (pre-upgrade)"
    # Reading must not consume it: `pin` still gets to adopt it properly.
    assert (tmp_path / "pinned.json").exists()
