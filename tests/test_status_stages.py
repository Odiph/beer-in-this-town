"""`status` per city: stage files, next_actions, blocked_on "emulator", hints.

The loop in AGENTS.md runs the first next_action and repeats. So every state
the pipeline can be in must hand back either one safe command that moves it
forward, or nothing -- with `blocked_on` saying whether nothing means
"finished" or "a person is needed". No network, no emulator.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from beer_in_this_town import cli, state
from beer_in_this_town.config import Settings, stage_path
from beer_in_this_town.emulator_checks import Check

pytestmark = pytest.mark.unit

CITY = "Tel Aviv"
LIST = "Tel Aviv Beer"
SAFE = {"status", "doctor", "verify", "sweep", "enrich", "filter", "export",
        "ui"}


def ready_checks(serial=None):
    return [Check(n, True, "ok", "") for n in
            ("adb_on_path", "device_connected", "untappd_installed",
             "screen_size")]


def no_adb(serial=None):
    from beer_in_this_town.emulator_checks import check_emulator

    return check_emulator(serial, which=lambda name: None,
                          run=lambda argv, t: (_ for _ in ()).throw(
                              AssertionError("adb must not be run")))


@pytest.fixture
def s(tmp_path):
    """Signed in, verified, and a city chosen."""
    session = tmp_path / "storage_state.json"
    session.write_text("{}", encoding="utf-8")
    state.record_verification({"google": {"ok": True},
                               "untappd": {"ok": True}}, ok=True)
    state.record_intent(CITY, LIST)
    return Settings(storage_state=session)


def write_stage(name, rows=2, age=0.0):
    path = stage_path(CITY, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "name\n" + "".join(f"v{i}\n" for i in range(rows))
    path.write_text(body, encoding="utf-8")
    if age:
        t = time.time() - age
        os.utime(path, (t, t))
    return path


def look(s, emulator=ready_checks):
    st = state.inspect_state(s, probe_emulator=True, emulator=emulator)
    st = {**st, "blocked_on": state.blocked_on(st)}
    return st, state.next_actions(st, s), state.hints(st, s)


def assert_safe(actions):
    for a in actions:
        assert a.startswith("python -m beer_in_this_town ")
        assert a.split()[3] in SAFE, a
        assert "#" not in a


# --- next_actions walks the stages ---------------------------------------

def test_nothing_run_yet_offers_the_sweep(s):
    st, actions, hints = look(s)
    assert st["next_stage"] == "sweep"
    assert actions == ['python -m beer_in_this_town sweep --city "Tel Aviv" --json']
    assert st["blocked_on"] is None
    assert any("Discover -> View Map" in h for h in hints)
    assert_safe(actions)


@pytest.mark.parametrize("have,nxt", [
    (["1_sweep.csv"], "enrich"),
    (["1_sweep.csv", "2_enriched.csv"], "filter"),
    (["1_sweep.csv", "2_enriched.csv", "3_venues.csv"], "export"),
])
def test_each_stage_offers_the_next_only_when_its_input_exists(s, have, nxt):
    for i, name in enumerate(have):
        write_stage(name, age=100 - i)          # older first: nothing stale
    st, actions, _ = look(s)
    assert st["next_stage"] == nxt
    assert actions == [f'python -m beer_in_this_town {nxt} --city "Tel Aviv" --json']
    assert st["emulator"] is None, "adb was probed although no sweep is next"


def test_everything_done_ends_the_loop_with_pin_and_notes_in_hints(s):
    for i, name in enumerate(["1_sweep.csv", "2_enriched.csv", "3_venues.csv"]):
        write_stage(name, age=100 - i)
    stage_path(CITY, "venues.kml").write_text("<kml/>", encoding="utf-8")
    st, actions, hints = look(s)
    assert actions == []
    assert st["blocked_on"] is None, "finished, not blocked"
    joined = " ".join(hints)
    venues = str(stage_path(CITY, "3_venues.csv"))
    assert f'pin --csv "{venues}" --list "{LIST}" --limit 3 --json' in joined
    assert "Saved -> New list" in joined
    assert "closures" in joined


def test_a_stale_stage_is_offered_again(s):
    write_stage("1_sweep.csv", age=0)
    write_stage("2_enriched.csv", age=3600)     # older than its input
    st, actions, _ = look(s)
    enrich = st["stages"][1]
    assert enrich["stale"] and not enrich["done"]
    assert actions[0].split()[3] == "enrich"


def test_stage_counts_and_paths_are_reported(s):
    write_stage("1_sweep.csv", rows=7)
    st, _, _ = look(s)
    sweep = st["stages"][0]
    assert sweep == {**sweep, "name": "sweep", "done": True, "count": 7}
    assert sweep["path"].endswith(os.path.join("tel-aviv", "1_sweep.csv"))
    assert [x["name"] for x in st["stages"]] == [
        "sweep", "enrich", "filter", "export", "pin", "notes"]


def test_an_interrupted_sweep_says_it_will_resume(s):
    from beer_in_this_town.app_sweep import journal_path

    journal_path(CITY).parent.mkdir(parents=True, exist_ok=True)
    journal_path(CITY).write_text(json.dumps(
        {"venues": [{"name": "a", "x": 1, "y": 1}] * 3}), encoding="utf-8")
    st, _, _ = look(s)
    assert "resumes" in st["stages"][0]["detail"]


def test_pin_and_notes_progress_comes_from_the_list_journals(s):
    from beer_in_this_town import notes, pin_to_list

    for name in ("1_sweep.csv", "2_enriched.csv", "3_venues.csv"):
        write_stage(name, rows=3)
    pin_to_list.journal_path(LIST).write_text(json.dumps(
        {"a": "ok", "b": "ok", "c": "failed"}), encoding="utf-8")
    notes.journal_path(LIST).write_text(json.dumps({"a": "ok"}),
                                        encoding="utf-8")
    st, actions, hints = look(s)
    pin, note = st["stages"][4], st["stages"][5]
    assert (pin["count"], pin["total"], pin["done"]) == (2, 3, False)
    assert pin["by_status"] == {"failed": 1, "ok": 2}
    assert note["count"] == 1
    assert not any(" pin " in f" {a} " or " notes " in f" {a} " for a in actions)
    assert any("1 failed" in h for h in hints)
    assert any(" notes --csv " in h for h in hints)


# --- the emulator gate ----------------------------------------------------

def test_an_unready_emulator_blocks_the_sweep(s):
    st, actions, hints = look(s, emulator=no_adb)
    assert st["blocked_on"] == "emulator"
    assert actions == [], "doctor changes nothing, so offering it would loop"
    joined = " ".join(hints)
    assert "adb_on_path" in joined and "platform-tools" in joined
    assert "Discover -> View Map" in joined


def test_status_works_with_no_adb_installed(s, monkeypatch, capsys):
    """The real check_emulator, with adb absent from PATH."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    env = cli.cmd_status(s)
    assert env.ok
    assert env.data["blocked_on"] == "emulator"
    assert env.data["emulator"][0]["name"] == "adb_on_path"


def test_the_emulator_is_not_blocking_once_the_sweep_is_done(s):
    write_stage("1_sweep.csv")
    st, actions, _ = look(s, emulator=no_adb)
    assert st["blocked_on"] is None
    assert actions and actions[0].split()[3] == "enrich"


def test_sign_in_still_comes_before_the_emulator(tmp_path):
    s = Settings(storage_state=tmp_path / "missing.json")
    state.record_intent(CITY)
    st, _, _ = look(s, emulator=no_adb)
    assert st["blocked_on"] == "sign_in"


def test_no_city_is_still_choose_city(tmp_path):
    session = tmp_path / "storage_state.json"
    session.write_text("{}", encoding="utf-8")
    state.record_verification({}, ok=True)
    s = Settings(storage_state=session)
    st, actions, hints = look(s)
    assert st["blocked_on"] == "choose_city"
    assert not any(" sweep " in f" {a} " for a in actions)
    assert any('sweep --city "<city>"' in h for h in hints)


def test_a_quote_in_a_city_cannot_break_the_command(tmp_path):
    session = tmp_path / "storage_state.json"
    session.write_text("{}", encoding="utf-8")
    state.record_verification({}, ok=True)
    state.record_intent('Evil" --i-read-robots "x')
    s = Settings(storage_state=session)
    _, actions, _ = look(s)
    assert actions == [
        'python -m beer_in_this_town sweep --city "Evil --i-read-robots x" --json']


def test_status_without_the_probe_never_runs_adb(s):
    def boom(serial=None):
        raise AssertionError("probed")

    st = state.inspect_state(s, emulator=boom)
    assert st["emulator"] is None
