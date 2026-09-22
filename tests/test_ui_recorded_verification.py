"""The dashboard should not ask a person to re-prove a failure.

Found live 2026-09-22: `verify` on the command line had just shown both
accounts signed out, and the page still offered "Test both accounts" and hid
the sign-in button behind it. A success is still re-proved on a restart --
that part is deliberate -- but a recorded failure is shown as what it is.

No browser, no network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.config import Settings
from beer_in_this_town.state import record_verification
from beer_in_this_town.ui import server
from beer_in_this_town.ui.checks import _google_check, _untappd_check

pytestmark = pytest.mark.unit


def _record(google_ok: bool, untappd_ok: bool = False, ran: bool = True):
    record_verification({
        "google": {"ok": google_ok, "ran": ran,
                   "detail": "The saved session is no longer signed in.",
                   "evidence": "No Google Account control on Maps"},
        "untappd": {"ok": untappd_ok, "ran": ran,
                    "detail": "No Untappd cookies in the saved session.",
                    "evidence": "storage_state carries no untappd.com cookies"},
    }, ok=google_ok and untappd_ok)


def test_a_recorded_failure_reaches_the_page():
    _record(google_ok=False)
    seeded = server._recorded_failures()
    assert set(seeded) == {"google", "untappd"}
    row = _google_check(Settings(), seeded["google"]).to_row()
    assert row["action"] == "connect", "the sign-in button must be offered"
    assert row["tested"] is True and row["verified"] is False


def test_a_recorded_success_is_still_re_proved():
    """A cookie that worked this morning can be dead by lunchtime."""
    _record(google_ok=True, untappd_ok=True)
    assert server._recorded_failures() == {}


def test_a_check_that_could_not_run_is_not_seeded():
    """Not a signed-out account, and it wants the opposite remedy."""
    _record(google_ok=False, untappd_ok=False, ran=False)
    assert server._recorded_failures() == {}


def test_no_record_seeds_nothing():
    assert server._recorded_failures() == {}


def test_tested_and_failed_is_not_untested():
    _record(google_ok=False)
    seeded = server._recorded_failures()
    assert _untappd_check(Settings(), seeded["untappd"]).to_row()["tested"]
