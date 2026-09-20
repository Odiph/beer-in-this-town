"""A cookie on disk is not a working account, and the panel must not say it is.

That is the whole claim this module makes, so it is what gets tested. The
failure it exists to prevent is the one `bootstrap` already shipped: a Google
session detected, success reported, and the first news of a signed-out Untappd
arriving a hundred requests into a run as `search_login_required`.

The last section walks a new user from nothing to ready, end to end, with the
round-trips stubbed. No network, no browser.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.config import Settings
from beer_in_this_town.ui import checks
from beer_in_this_town.ui.actions import verify_accounts
from beer_in_this_town.ui.checks import ATTENTION, OK, UNKNOWN, VerifyResult


@pytest.fixture
def blank(tmp_path):
    """A machine that has never run this tool."""
    return Settings(profile_dir=tmp_path / "profile",
                    storage_state=tmp_path / "storage_state.json")


def _row(rows, key):
    return next(c for c in rows if c.key == key)


# --- detection is not verification ---------------------------------------

@pytest.mark.unit
def test_a_saved_session_alone_is_never_reported_as_working(blank, monkeypatch):
    """The exact mistake bootstrap makes. A cookie is evidence, not proof."""
    monkeypatch.setattr("beer_in_this_town.chrome_launch.profile_has_google_session",
                        lambda d: True)
    blank.storage_state.write_text("{}", encoding="utf-8")

    google = _row(checks.collect(blank), "google")
    assert google.state == UNKNOWN
    assert google.verified is False
    assert "not tested" in google.detail


@pytest.mark.unit
def test_a_verified_session_is_the_only_thing_that_reads_as_ok(blank):
    proven = {"google": VerifyResult(True, "Signed in — checked just now.")}
    google = _row(checks.collect(blank, proven), "google")
    assert google.state == OK
    assert google.verified is True


@pytest.mark.unit
def test_a_session_that_stopped_working_asks_for_a_new_sign_in(blank):
    proven = {"google": VerifyResult(False, "The saved session is no longer "
                                            "signed in.")}
    google = _row(checks.collect(blank, proven), "google")
    assert google.state == ATTENTION
    assert google.action == "connect"
    assert google.verified is False


@pytest.mark.unit
def test_nothing_connected_reads_as_attention_not_unknown(blank):
    rows = checks.collect(blank)
    assert _row(rows, "google").state == ATTENTION
    assert _row(rows, "untappd").state == ATTENTION


@pytest.mark.unit
def test_an_unreadable_profile_is_unknown_not_missing(blank, monkeypatch):
    """Chrome holding the cookie store must not read as "signed out".

    Saying "not connected" there sends the user round a login loop they have
    already finished.
    """
    blank.profile_dir.mkdir(parents=True)   # a profile exists; it just won't open
    monkeypatch.setattr("beer_in_this_town.chrome_launch.profile_has_google_session",
                        lambda d: None)
    assert _row(checks.collect(blank), "google").state == UNKNOWN


# --- ready is strict ------------------------------------------------------

@pytest.mark.unit
def test_ready_needs_both_accounts_proven(blank, monkeypatch):
    monkeypatch.setattr("beer_in_this_town.chrome_launch.find_chrome",
                        lambda: __import__("pathlib").Path("chrome"))
    monkeypatch.setattr("beer_in_this_town.chrome_launch.chrome_major_version",
                        lambda: "151")

    one = {"google": VerifyResult(True, "ok")}
    assert checks.ready(checks.collect(blank, one)) is False

    both = {"google": VerifyResult(True, "ok"),
            "untappd": VerifyResult(True, "ok")}
    assert checks.ready(checks.collect(blank, both)) is True


@pytest.mark.unit
def test_a_missing_places_key_is_off_not_broken(blank):
    """Declining an optional paid API is a choice, not a fault."""
    places = _row(checks.collect(blank), "places")
    assert places.state == "off"
    assert places not in checks.blocking(checks.collect(blank))


# --- the new user, end to end --------------------------------------------

@pytest.mark.unit
def test_a_new_user_goes_from_nothing_to_ready(blank, monkeypatch):
    """The whole first-run arc, with the two round-trips stubbed.

    Written as one test on purpose: the individual states above can each be
    right while the sequence still strands someone — which is what it means
    for this to be an end-to-end check rather than four unit assertions.
    """
    monkeypatch.setattr("beer_in_this_town.chrome_launch.find_chrome",
                        lambda: __import__("pathlib").Path("chrome"))
    monkeypatch.setattr("beer_in_this_town.chrome_launch.chrome_major_version",
                        lambda: "151")

    # 1. Fresh machine: both accounts need the user, and nothing claims to work.
    rows = checks.collect(blank)
    assert {c.key for c in checks.blocking(rows)} == {"google", "untappd"}
    assert checks.ready(rows) is False
    assert all(not c.verified for c in rows if c.key in ("google", "untappd"))

    # 2. They sign in. Chrome writes the profile, the cookies land, and the
    #    panel still refuses to call it working, because nothing was tested.
    blank.profile_dir.mkdir(parents=True)
    monkeypatch.setattr("beer_in_this_town.chrome_launch.profile_has_google_session",
                        lambda d: True)
    monkeypatch.setattr("beer_in_this_town.chrome_launch.profile_has_untappd_session",
                        lambda d: True)
    blank.storage_state.write_text("{}", encoding="utf-8")

    rows = checks.collect(blank)
    assert checks.ready(rows) is False, "a cookie was accepted as a working account"
    assert not checks.blocking(rows), "signing in left something marked broken"

    # 3. They press Test. Both round-trips answer.
    said = []
    monkeypatch.setitem(checks.VERIFIERS, "google",
                        lambda s: VerifyResult(True, "google ok", "stub"))
    monkeypatch.setitem(checks.VERIFIERS, "untappd",
                        lambda s: VerifyResult(True, "untappd ok", "stub"))
    result = verify_accounts(blank, lambda t, aside=False: said.append(t))

    assert result["google"]["ok"] is True
    assert result["untappd"]["ok"] is True
    assert any("2 of 2" in t for t in said), "the user was not told the outcome"

    # 4. Ready, and only now.
    proven = {k: VerifyResult(**v) for k, v in result.items()}
    rows = checks.collect(blank, proven)
    assert checks.ready(rows) is True
    assert all(_row(rows, k).verified for k in ("google", "untappd"))


@pytest.mark.unit
def test_a_failing_untappd_login_blocks_ready_and_says_why(blank, monkeypatch):
    """The half that used to be discovered a hundred requests into a run."""
    monkeypatch.setattr("beer_in_this_town.chrome_launch.find_chrome",
                        lambda: __import__("pathlib").Path("chrome"))
    monkeypatch.setattr("beer_in_this_town.chrome_launch.chrome_major_version",
                        lambda: "151")

    proven = {"google": VerifyResult(True, "Signed in — checked just now."),
              "untappd": VerifyResult(False, "Reached Untappd, but signed out.")}
    rows = checks.collect(blank, proven)

    assert checks.ready(rows) is False
    untappd = _row(rows, "untappd")
    assert untappd.state == ATTENTION
    assert untappd.action == "connect"
    assert "untappd.com" in untappd.fix


# --- a probe that could not run is not a signed-out account ---------------

@pytest.mark.unit
def test_a_browser_that_will_not_start_does_not_ask_for_a_new_login(blank):
    """Both are ok=False and they want opposite advice.

    Telling someone to sign in again when Playwright is missing sends them
    through a login that was never the problem — the same distinction
    `GeocoderUnavailable` draws between a broken integration and a bad row,
    one layer further up.
    """
    proven = {"google": VerifyResult(False, "Could not run the check.",
                                     evidence="Run: pip install -e \".[browser]\"",
                                     ran=False)}
    google = _row(checks.collect(blank, proven), "google")

    assert google.state == UNKNOWN, "a broken browser read as a signed-out account"
    assert google.action == "verify"
    assert "sign in" not in google.fix.lower()
    assert "playwright" in google.fix.lower() or "install" in google.fix.lower()


@pytest.mark.unit
def test_an_unreachable_untappd_does_not_ask_for_a_new_login(blank):
    proven = {"untappd": VerifyResult(False, "Could not reach Untappd.",
                                      evidence="ConnectError: no route",
                                      ran=False)}
    untappd = _row(checks.collect(blank, proven), "untappd")
    assert untappd.state == UNKNOWN
    assert untappd.action == "verify"


@pytest.mark.unit
def test_a_probe_that_ran_and_failed_still_asks_for_a_login(blank):
    """The other direction: a real signed-out session must not read as unknown."""
    proven = {"untappd": VerifyResult(False, "Reached Untappd, but signed out.",
                                      evidence="no signed-in marker")}
    untappd = _row(checks.collect(blank, proven), "untappd")
    assert untappd.state == ATTENTION
    assert untappd.action == "connect"


# --- what a brand-new user is told to do ---------------------------------

@pytest.mark.unit
def test_a_user_with_no_session_is_not_sent_to_run(blank):
    """`run` signed out of Untappd builds a five-venue corpus and calls it done.

    The old first action was exactly that, on the reasoning that a session
    was only needed for the YOU column. The 5-result search cap makes that
    false, and a truncated scrape is worse than no scrape because the diff,
    the baseline and the KML are then all wrong together.
    """
    from beer_in_this_town.state import inspect_state, next_actions

    state = inspect_state(blank)
    actions = next_actions(state, blank)

    assert state["logged_in"] is False
    assert not any("run " in a for a in actions), \
        "a signed-out user was pointed at run"


@pytest.mark.unit
def test_the_agent_opens_the_dashboard_then_stops(blank, monkeypatch):
    """Walk the loop. Both ends of this have bitten.

    Handing back `selfcheck` never terminated, because nothing selfcheck does
    changes the session. Handing back nothing at all stranded the user: the
    agent finished the install, reported "ready", and the one thing that
    would have told them what to do next was the thing it had been told not
    to run.

    So the first pass opens the dashboard, and the second -- with one already
    serving -- ends the loop.
    """
    import beer_in_this_town.ui as ui
    from beer_in_this_town.state import blocked_on, inspect_state, next_actions

    state = inspect_state(blank)

    monkeypatch.setattr(ui, "existing", lambda: None)
    first = next_actions(state, blank)
    assert first == ["python -m beer_in_this_town ui --detach --json"]
    assert "--detach" in first[0], "an agent was handed the blocking form"

    monkeypatch.setattr(ui, "existing",
                        lambda: {"url": "http://127.0.0.1:8765/#k", "pid": 1,
                                 "port": 8765})
    for _ in range(3):                      # the loop, three times round
        assert next_actions(state, blank) == [], \
            "the agent relaunches a dashboard that is already running"

    assert blocked_on(state) == "sign_in", \
        "an empty list with no reason is indistinguishable from finished"


@pytest.mark.unit
def test_an_agent_is_told_how_to_check_the_sign_in_took(blank):
    """`ui` blocks on a person; `verify` is the part an agent can run."""
    from beer_in_this_town.state import hints, inspect_state

    said = " ".join(hints(inspect_state(blank), blank))
    assert "verify --json" in said
    assert "no agent can do it" in said


@pytest.mark.unit
def test_the_first_hint_names_the_dashboard_and_both_accounts(blank):
    from beer_in_this_town.state import hints, inspect_state

    said = " ".join(hints(inspect_state(blank), blank))
    assert "beertown ui" in said
    assert "Untappd" in said, "only Google was mentioned, which is the old bug"
    assert "5 results" in said


@pytest.mark.unit
def test_nothing_in_next_actions_can_touch_an_account(blank):
    """Whatever is in next_actions is, in effect, authorised. AGENTS.md rule 2.

    `ui` is allowed here now, and only in its `--detach` form: it returns
    instead of blocking, serves a read-only page bound to localhost, and has
    no route that can write to a saved list. The blocking form would hang the
    agent, which is the whole reason it used to be excluded.
    """
    from beer_in_this_town.state import inspect_state, next_actions

    actions = next_actions(inspect_state(blank), blank)
    joined = f" {' '.join(actions)} "
    for forbidden in (" pin ", " notes ", "bootstrap"):
        assert forbidden not in joined
    for action in actions:
        if " ui " in f" {action} ":
            assert "--detach" in action, "the blocking dashboard was offered"


# --- the dashboard leads, rather than reporting --------------------------

def _steps_for(s, proven=None):
    return checks.next_step(checks.collect(s, proven))


@pytest.mark.unit
def test_a_new_user_is_told_to_sign_in_not_shown_a_checklist(blank, monkeypatch):
    monkeypatch.setattr("beer_in_this_town.chrome_launch.find_chrome",
                        lambda: __import__("pathlib").Path("chrome"))
    monkeypatch.setattr("beer_in_this_town.chrome_launch.chrome_major_version",
                        lambda: "151")
    step = _steps_for(blank)
    assert step.key == "connect"
    assert step.action == "connect"
    assert step.cta
    # Both accounts named, so the Untappd half cannot be quietly skipped.
    assert "Google" in step.body and "Untappd" in step.body


@pytest.mark.unit
def test_a_missing_chrome_outranks_the_sign_in(blank, monkeypatch):
    """No point telling someone to sign in when the window cannot open."""
    monkeypatch.setattr("beer_in_this_town.chrome_launch.find_chrome",
                        lambda: None)
    step = _steps_for(blank)
    assert step.key == "chrome"
    assert step.action == "", "offered a button for something we cannot do"


@pytest.mark.unit
def test_signed_in_but_untested_leads_to_the_test(blank, monkeypatch):
    for name, value in (("find_chrome", lambda: __import__("pathlib").Path("c")),
                        ("chrome_major_version", lambda: "151"),
                        ("profile_has_google_session", lambda d: True),
                        ("profile_has_untappd_session", lambda d: True)):
        monkeypatch.setattr(f"beer_in_this_town.chrome_launch.{name}", value)
    blank.profile_dir.mkdir(parents=True)
    blank.storage_state.write_text("{}", encoding="utf-8")

    step = _steps_for(blank)
    assert step.key == "verify"
    assert step.action == "verify"
    assert step.done is False


@pytest.mark.unit
def test_only_a_verified_setup_asks_for_the_city(blank, monkeypatch):
    """The city question is the last step, and it is gated on both accounts.

    Asking for a city while Untappd is signed out would hand someone a
    command that builds a five-venue corpus.
    """
    monkeypatch.setattr("beer_in_this_town.chrome_launch.find_chrome",
                        lambda: __import__("pathlib").Path("c"))
    monkeypatch.setattr("beer_in_this_town.chrome_launch.chrome_major_version",
                        lambda: "151")

    half = {"google": VerifyResult(True, "ok")}
    assert _steps_for(blank, half).key != "run"

    both = {"google": VerifyResult(True, "ok"),
            "untappd": VerifyResult(True, "ok")}
    step = _steps_for(blank, both)
    assert step.key == "run"
    assert step.done is True
    assert "city" in step.title.lower()


@pytest.mark.unit
def test_the_page_offers_a_way_to_make_an_account(blank):
    """Someone with no Untappd account cannot "sign in" to one."""
    page = (__import__("pathlib").Path("beer_in_this_town/ui/index.html")
            .read_text(encoding="utf-8"))
    assert "untappd.com/signup" in page
    assert "accounts.google.com/signup" in page
