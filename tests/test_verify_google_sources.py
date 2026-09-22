"""Two places hold the Google session, and they can disagree.

Measured 2026-09-22: `verify` said Google was signed out while `pin`'s own
pre-flight, minutes later and against the Chrome profile, was fine. Google
rotates its session cookies, so the snapshot `enrich` reads with can go stale
while the profile it came from is signed in. Reporting that as "signed out"
sends a person through a login they have already done.

No browser, no network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.config import Settings
from beer_in_this_town.ui import checks
from beer_in_this_town.ui.checks import VerifyResult, verify_google

pytestmark = pytest.mark.unit

SIGNED_IN = VerifyResult(True, "Signed in — checked just now.", evidence="x")
SIGNED_OUT = VerifyResult(False, "The saved session is no longer signed in.",
                          evidence="No Google Account control on Maps")
COULD_NOT_RUN = VerifyResult(False, "Could not run the check.",
                             evidence="ImportError", ran=False)


@pytest.fixture
def sources(monkeypatch):
    def use(snapshot, profile=None):
        calls = []

        def _snapshot(s):
            calls.append("snapshot")
            return snapshot

        def _profile(s):
            calls.append("profile")
            if profile is None:
                raise AssertionError("the profile must not be opened here")
            return profile

        monkeypatch.setattr(checks, "_google_via_snapshot", _snapshot)
        monkeypatch.setattr(checks, "_google_via_profile", _profile)
        return calls
    return use


def test_a_good_snapshot_is_enough(sources):
    calls = sources(SIGNED_IN)
    assert verify_google(Settings()).ok
    assert calls == ["snapshot"], "no need to open the profile"


def test_a_stale_snapshot_is_settled_by_the_profile(sources):
    calls = sources(SIGNED_OUT, profile=VerifyResult(
        True, "Signed in — checked just now, in the Chrome profile.",
        evidence="refreshed"))
    result = verify_google(Settings())
    assert result.ok and "profile" in result.detail
    assert calls == ["snapshot", "profile"]


def test_signed_out_in_both_is_signed_out(sources):
    sources(SIGNED_OUT, profile=VerifyResult(
        False, "The Chrome profile is not signed in.", evidence="none"))
    result = verify_google(Settings())
    assert not result.ok and result.ran


def test_a_locked_profile_does_not_overturn_the_snapshot(sources):
    """Chrome holds a lock while a window is open. That is not a verdict."""
    sources(SIGNED_OUT, profile=VerifyResult(
        False, "Could not read the Chrome profile.", evidence="lock",
        ran=False))
    result = verify_google(Settings())
    assert not result.ok and result.ran
    assert "saved session" in result.detail


def test_a_check_that_could_not_run_is_not_a_login_problem(sources):
    calls = sources(COULD_NOT_RUN)
    result = verify_google(Settings())
    assert result.ran is False
    assert calls == ["snapshot"], "nothing to settle: the check never ran"
