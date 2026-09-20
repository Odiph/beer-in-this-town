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
