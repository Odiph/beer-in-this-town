"""Recorded consent for the two commands that write to a Google account (#22).

AGENTS.md rule 2 said "never run `pin` without an explicit human instruction"
and nothing checked it. These tests pin down the check that replaces the
prose: a per-list record only a person at a terminal can create, read fail
closed, and consulted by `pin` and `notes` before any browser work.

All offline. The sandbox fixture in conftest.py redirects `state/`.
"""
from __future__ import annotations

import io
import json
import sys

import pytest

from beer_in_this_town import cli, config, consent, state

pytestmark = pytest.mark.unit

LIST = "London Bars"
DAY = 24 * 3600
T0 = 1_800_000_000.0


class Tty(io.StringIO):
    """A stdin that says it is a terminal and answers with `typed`."""

    def isatty(self) -> bool:
        return True


class Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _raw(payload) -> None:
    path = consent.consent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                    encoding="utf-8")


# --- the record ------------------------------------------------------------

def test_the_record_lives_in_the_sandboxed_state_dir(sandbox_state):
    """Resolved at call time, so the sandbox (and a relocated install) wins."""
    assert consent.consent_path().is_relative_to(sandbox_state)
    assert consent.consent_path().name == "consent.json"


def test_no_file_means_no_consent():
    assert consent.has_consent(LIST) is False


def test_a_grant_covers_its_own_list():
    rec = consent.grant(LIST, days=7, now=T0)
    assert rec["list"] == LIST
    assert rec["expires_at"] == T0 + 7 * DAY
    assert consent.has_consent(LIST, now=T0 + 1) is True


def test_the_write_is_atomic_and_leaves_no_temp_file():
    consent.grant(LIST, now=T0)
    leftovers = [p.name for p in consent.consent_path().parent.iterdir()
                 if p.name.endswith(".tmp")]
    assert leftovers == []
    body = json.loads(consent.consent_path().read_text(encoding="utf-8"))
    assert body[config.scope_slug(LIST)]["list"] == LIST


def test_consent_expires():
    consent.grant(LIST, days=7, now=T0)
    assert consent.has_consent(LIST, now=T0 + 7 * DAY - 1) is True
    assert consent.has_consent(LIST, now=T0 + 7 * DAY + 1) is False


def test_the_default_window_is_seven_days():
    rec = consent.grant(LIST, now=T0)
    assert rec["expires_at"] - rec["granted_at"] == 7 * DAY


@pytest.mark.parametrize("days", [0, -1, 31, 365])
def test_the_window_is_bounded(days):
    with pytest.raises(ValueError):
        consent.grant(LIST, days=days, now=T0)
    assert consent.has_consent(LIST, now=T0) is False


@pytest.mark.parametrize("other", ["London Bars Test", "London", "london bars",
                                   "London  Bars", "Tel Aviv Beer"])
def test_consent_is_for_the_exact_list_only(other):
    """Consent for one list is not consent for a similarly named one."""
    consent.grant(LIST, now=T0)
    assert consent.has_consent(other, now=T0 + 1) is False


def test_two_lists_are_recorded_independently():
    consent.grant(LIST, now=T0)
    consent.grant("Tel Aviv Beer", days=2, now=T0)
    consent.revoke(LIST)
    assert consent.has_consent(LIST, now=T0 + 1) is False
    assert consent.has_consent("Tel Aviv Beer", now=T0 + 1) is True


def test_revoke_removes_it():
    consent.grant(LIST, now=T0)
    assert consent.revoke(LIST) is True
    assert consent.has_consent(LIST, now=T0 + 1) is False
    assert consent.revoke(LIST) is False, "nothing left to revoke"


def test_a_blank_list_name_cannot_be_granted():
    with pytest.raises(ValueError):
        consent.grant("   ", now=T0)


# --- fail closed -------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "{not json",
    "",
    "[]",
    '"yes"',
    json.dumps({config.scope_slug(LIST): "granted"}),
    json.dumps({config.scope_slug(LIST): {"list": LIST}}),
    json.dumps({config.scope_slug(LIST): {"list": LIST, "granted_at": "x",
                                          "expires_at": "y"}}),
    json.dumps({config.scope_slug(LIST): {"list": 7, "granted_at": T0,
                                          "expires_at": T0 + DAY}}),
])
def test_a_corrupt_record_means_no_consent(payload):
    _raw(payload)
    assert consent.has_consent(LIST, now=T0 + 1) is False


def test_an_unreadable_record_means_no_consent():
    path = consent.consent_path()
    path.mkdir(parents=True)              # a directory where the file should be
    assert consent.has_consent(LIST, now=T0 + 1) is False


def test_a_record_filed_under_the_wrong_key_means_no_consent():
    _raw({config.scope_slug(LIST): {"list": "Somewhere Else",
                                    "granted_at": T0, "expires_at": T0 + DAY}})
    assert consent.has_consent(LIST, now=T0 + 1) is False


def test_a_record_longer_than_the_maximum_window_is_not_honoured():
    """A hand-edited year of consent is not something `allow-writes` grants."""
    _raw({config.scope_slug(LIST): {"list": LIST, "granted_at": T0,
                                    "expires_at": T0 + 365 * DAY}})
    assert consent.has_consent(LIST, now=T0 + 1) is False


def test_a_record_granted_in_the_future_is_not_honoured():
    _raw({config.scope_slug(LIST): {"list": LIST, "granted_at": T0 + DAY,
                                    "expires_at": T0 + 2 * DAY}})
    assert consent.has_consent(LIST, now=T0) is False


def test_a_grant_over_a_corrupt_file_replaces_it():
    _raw("{not json")
    consent.grant(LIST, now=T0)
    assert consent.has_consent(LIST, now=T0 + 1) is True


# --- allow-writes: only a person at a terminal --------------------------------

def _envelope(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_allow_writes_refuses_json(monkeypatch, capsys):
    """An agent speaks --json; a grant must not be available to it."""
    monkeypatch.setattr(sys, "stdin", Tty(LIST + "\n"))
    assert cli.main(["allow-writes", "--list", LIST, "--json"]) == 1
    env = _envelope(capsys)
    assert env["error"]["code"] == "human_only"
    assert consent.has_consent(LIST) is False
    assert not consent.consent_path().exists()


def test_allow_writes_refuses_json_before_the_subcommand(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Tty(LIST + "\n"))
    assert cli.main(["--json", "allow-writes", "--list", LIST]) == 1
    assert _envelope(capsys)["error"]["code"] == "human_only"
    assert consent.has_consent(LIST) is False


def test_allow_writes_refuses_without_a_terminal(monkeypatch, capsys):
    """Piped stdin is automation, even when it types the right name."""
    monkeypatch.setattr(sys, "stdin", Pipe(LIST + "\n"))
    assert cli.main(["allow-writes", "--list", LIST]) == 1
    out = capsys.readouterr().out
    assert "[FAILED] allow-writes" in out and "terminal" in out
    assert consent.has_consent(LIST) is False


def test_allow_writes_records_consent_when_the_name_is_typed(monkeypatch,
                                                             capsys):
    monkeypatch.setattr(sys, "stdin", Tty(LIST + "\n"))
    assert cli.main(["allow-writes", "--list", LIST, "--days", "3"]) == 0
    out = capsys.readouterr().out
    # The person was told what they are agreeing to before being asked.
    assert "Terms of Service" in out
    assert "pin" in out and "notes" in out
    assert consent.has_consent(LIST) is True
    rec = consent.active_consents()[0]
    assert round((rec["expires_at"] - rec["granted_at"]) / DAY) == 3


@pytest.mark.parametrize("typed", ["london bars", "London Bars Test", "y",
                                   "yes", ""])
def test_allow_writes_needs_the_exact_name(monkeypatch, capsys, typed):
    monkeypatch.setattr(sys, "stdin", Tty(typed + "\n"))
    assert cli.main(["allow-writes", "--list", LIST]) == 1
    assert consent.has_consent(LIST) is False


def test_allow_writes_on_closed_stdin_records_nothing(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Tty(""))      # EOF at the prompt
    assert cli.main(["allow-writes", "--list", LIST]) == 1
    assert consent.has_consent(LIST) is False


@pytest.mark.parametrize("days", ["0", "31"])
def test_allow_writes_bounds_the_window(monkeypatch, capsys, days):
    monkeypatch.setattr(sys, "stdin", Tty(LIST + "\n"))
    with pytest.raises(SystemExit) as exit_:
        cli.main(["allow-writes", "--list", LIST, "--days", days])
    assert exit_.value.code == 2
    assert "--days must be between 1 and 30" in capsys.readouterr().out
    assert consent.has_consent(LIST) is False


def test_revoke_works_from_anywhere(monkeypatch, capsys):
    """Taking permission away is always safe, so it needs no terminal."""
    consent.grant(LIST)
    monkeypatch.setattr(sys, "stdin", Pipe(""))
    assert cli.main(["allow-writes", "--revoke", "--list", LIST,
                     "--json"]) == 0
    env = _envelope(capsys)
    assert env["ok"] is True and env["data"]["revoked"] is True
    assert consent.has_consent(LIST) is False


# --- pin and notes check it before any browser work ---------------------------

@pytest.fixture
def no_browser(monkeypatch):
    """Fail the test if anything reaches a browser or the write loop."""
    def boom(*a, **k):
        raise AssertionError("reached the browser without consent")

    monkeypatch.setattr(cli, "pin_places", boom)
    monkeypatch.setattr(cli, "add_notes", boom)
    # And the real entry point underneath, should anything bypass the names.
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)


@pytest.fixture
def venues(tmp_path):
    path = tmp_path / "3_venues.csv"
    path.write_text("name,address,city,total,unique,monthly\n"
                    "The Rake,14 Winchester Walk,London,71162,11112,345\n",
                    encoding="utf-8")
    return path


@pytest.mark.parametrize("cmd", ["pin", "notes"])
def test_a_write_without_consent_is_refused_before_the_browser(
        cmd, venues, no_browser, capsys):
    rc = cli.main([cmd, "--csv", str(venues), "--list", LIST, "--limit", "3",
                   "--json"])
    assert rc == 1
    env = _envelope(capsys)
    assert env["ok"] is False
    assert env["error"]["code"] == "no_consent"
    assert f'allow-writes --list "{LIST}"' in env["error"]["remedy"]
    assert env["next_actions"] == []
    assert not any("allow-writes" in a for a in env["next_actions"])


@pytest.mark.parametrize("cmd", ["pin", "notes"])
def test_consent_for_another_list_does_not_cover_this_one(
        cmd, venues, no_browser, capsys):
    consent.grant("London Bars Test")
    assert cli.main([cmd, "--csv", str(venues), "--list", LIST,
                     "--json"]) == 1
    assert _envelope(capsys)["error"]["code"] == "no_consent"


@pytest.mark.parametrize("cmd", ["pin", "notes"])
def test_expired_consent_is_refused(cmd, venues, no_browser, capsys,
                                    monkeypatch):
    consent.grant(LIST, days=1, now=T0)
    monkeypatch.setattr(consent.time, "time", lambda: T0 + 2 * DAY)
    assert cli.main([cmd, "--csv", str(venues), "--list", LIST,
                     "--json"]) == 1
    assert _envelope(capsys)["error"]["code"] == "no_consent"


@pytest.mark.parametrize("cmd,entry", [("pin", "pin_places"),
                                       ("notes", "add_notes")])
def test_with_consent_the_write_goes_ahead(cmd, entry, venues, capsys,
                                           monkeypatch):
    seen = []
    monkeypatch.setattr(cli, entry,
                        lambda places, s, list_name, **k: seen.append(list_name)
                        or {})
    consent.grant(LIST)
    assert cli.main([cmd, "--csv", str(venues), "--list", LIST,
                     "--json"]) == 0
    assert seen == [LIST]


# --- status ------------------------------------------------------------------

def test_status_reports_consent_per_list(tmp_path):
    consent.grant(LIST, days=2)
    st = state.inspect_state(config.Settings())
    lists = st["write_guardrails"]["consent"]
    assert [c["list"] for c in lists] == [LIST]
    assert 0 < lists[0]["remaining_h"] <= 48


def test_status_reports_no_consent_as_an_empty_list():
    st = state.inspect_state(config.Settings())
    assert st["write_guardrails"]["consent"] == []


def test_allow_writes_is_named_in_hints_and_never_in_next_actions(tmp_path):
    from beer_in_this_town.config import stage_path

    session = tmp_path / "storage_state.json"
    session.write_text("{}", encoding="utf-8")
    state.record_verification({"google": {"ok": True},
                               "untappd": {"ok": True}}, ok=True)
    state.record_intent("Tel Aviv", "Tel Aviv Beer")
    for name in ("1_sweep.csv", "2_enriched.csv", "3_venues.csv"):
        path = stage_path("Tel Aviv", name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("name\nv\n", encoding="utf-8")
    stage_path("Tel Aviv", "venues.kml").write_text("<kml/>", encoding="utf-8")
    s = config.Settings(storage_state=session)

    st = state.inspect_state(s)
    st = {**st, "blocked_on": state.blocked_on(st)}
    actions = state.next_actions(st, s)
    hints = " ".join(state.hints(st, s))
    assert not any("allow-writes" in a for a in actions)
    assert 'allow-writes --list "Tel Aviv Beer"' in hints

    consent.grant("Tel Aviv Beer")
    st = state.inspect_state(s)
    st = {**st, "blocked_on": state.blocked_on(st)}
    assert "allow-writes --list" not in " ".join(state.hints(st, s))
    assert not any("allow-writes" in a for a in state.next_actions(st, s))


def test_a_remedy_naming_allow_writes_is_never_promoted_to_a_next_action():
    from beer_in_this_town.agent_io import Problem, fail

    env = fail("pin", Problem(
        code="no_consent", message="x",
        remedy=f'python -m beer_in_this_town allow-writes --list "{LIST}"'))
    assert env.next_actions == []
    assert env.hints and "allow-writes" in env.hints[0]


# --- the dashboard shows the command and cannot run it -------------------------

def test_the_dashboard_shows_the_consent_command_as_text():
    from beer_in_this_town.ui import flow

    card = flow.maps_card("Tel Aviv", "Tel Aviv Beer")
    assert card["commands"]["allow_writes"] == (
        'beertown allow-writes --list "Tel Aviv Beer"')


def test_nothing_in_the_dashboard_can_grant_consent():
    """Consent stays on the terminal: no dashboard module touches the record."""
    from pathlib import Path

    import beer_in_this_town.ui as ui

    sources = list(Path(ui.__file__).parent.glob("*.py"))
    assert sources
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert "import consent" not in text, source.name
        assert "from ..consent" not in text, source.name
        assert "consent.grant" not in text, source.name


def test_allow_writes_with_a_blank_list_is_a_clean_refusal(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Tty("\n"))
    assert cli.main(["allow-writes", "--list", "  "]) == 1
    assert "--list is empty" in capsys.readouterr().out
    assert consent.active_consents() == []
