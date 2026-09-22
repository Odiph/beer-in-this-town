"""The sandbox in conftest is itself load-bearing, so it gets tests.

A guard nobody checks is the same shape as the `selfcheck` problem: something
reassuring that cannot see the failure it exists to catch.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from beer_in_this_town import export, geocode, notes, pin_to_list, state
from beer_in_this_town.config import DATA_DIR, STATE_DIR, Settings


@pytest.mark.unit
@pytest.mark.parametrize("module,attr", [
    (state, "LAST_RUN"),
    (state, "LEGACY_PINNED"),
    (geocode, "GEOCACHE"),
    (export, "LEGACY_PREVIOUS_RUN"),
    (pin_to_list, "LEGACY_JOURNAL"),
    (notes, "LEGACY_NOTE_JOURNAL"),
])
def test_constants_resolved_at_import_are_redirected(module, attr, sandbox_state):
    """These are exactly the ones patching STATE_DIR alone does not reach."""
    value = getattr(module, attr)
    assert value.is_relative_to(sandbox_state), value
    assert not value.is_relative_to(STATE_DIR), f"{attr} still points at state/"


@pytest.mark.unit
def test_the_directories_themselves_are_redirected(sandbox_state):
    assert state.STATE_DIR.is_relative_to(sandbox_state)
    from beer_in_this_town import config

    assert config.DATA_DIR.is_relative_to(sandbox_state)
    assert config.stage_path("london", "1_sweep.csv").is_relative_to(
        sandbox_state)


@pytest.mark.unit
def test_session_presence_is_fixed_not_inherited(sandbox_state):
    """The CI-only failure: logged_in depended on the developer's machine."""
    s = Settings()
    assert s.storage_state.is_relative_to(sandbox_state)
    assert s.storage_state.exists() is False, "tests must start signed out"


@pytest.mark.unit
def test_writing_through_a_redirected_constant_lands_in_the_sandbox(sandbox_state):
    """The end-to-end property: a real write goes nowhere near the project.

    Asserted as "the real file is untouched" rather than "the real file does
    not exist": on a machine that has actually run the tool it does exist,
    and this failed there while passing in a fresh clone -- the machine
    dependence this file is here to stop.
    """
    real = STATE_DIR / "last_run.json"
    before = real.read_bytes() if real.exists() else None

    state.record_run(query="london", map_title="London Bars",
                     csv_path=Path("venues_london.csv"))

    assert state.LAST_RUN.exists()
    assert state.LAST_RUN.is_relative_to(sandbox_state)
    assert (real.read_bytes() if real.exists() else None) == before


@pytest.mark.unit
def test_the_escape_detector_sees_a_file_that_should_not_be_there(tmp_path):
    """Directly exercise the teardown sweep, since it cannot fail its own test."""
    from tests.conftest import _snapshot

    root = tmp_path / "state"
    root.mkdir()
    before = _snapshot([root])
    (root / "escaped.json").write_text("{}", encoding="utf-8")
    assert _snapshot([root]) - before, "the sweep must notice a new file"


@pytest.mark.unit
def test_the_guard_must_capture_real_roots_before_redirecting(sandbox_state):
    """config.STATE_DIR is itself redirected, so order is load-bearing.

    Asking for the real roots during a test returns the sandbox. If the guard
    captured them at teardown instead of at setup it would compare tmp_path
    against tmp_path, find nothing, and pass for every escape there is.
    """
    from tests.conftest import _real_roots

    assert set(_real_roots()).isdisjoint({DATA_DIR, STATE_DIR})
    assert all(r.is_relative_to(sandbox_state) for r in _real_roots())
