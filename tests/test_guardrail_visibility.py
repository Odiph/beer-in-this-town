"""`status` has to be able to answer what its own remedies send agents to it for.

Several error remedies say "check `status`" for a cool-off, and AGENTS.md's
`already_running` row admits status cannot see the lock. So an agent that hits
a cool-off asks status, is told everything is clear, and re-runs straight back
into the trip. Offline: these read files, nothing else.
"""
from __future__ import annotations

import json
import time

import pytest

from beer_in_this_town.guardrails import Limits, inspect_guardrails


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from beer_in_this_town import guardrails
    path = tmp_path / "rate_ledger.json"
    monkeypatch.setattr(guardrails, "LEDGER", path)
    return path


@pytest.mark.unit
def test_nothing_on_disk_reads_as_a_clean_slate(ledger):
    report = inspect_guardrails(Limits(), path=ledger)
    assert report["used_today"] == 0
    assert report["cooloff_remaining_h"] == 0
    assert report["lock"] is None


@pytest.mark.unit
def test_an_active_cooloff_is_visible(ledger):
    ledger.write_text(json.dumps(
        {"events": [], "blocked_until": time.time() + 3 * 3600}), encoding="utf-8")
    report = inspect_guardrails(Limits(), path=ledger)
    assert 2.9 < report["cooloff_remaining_h"] < 3.1
    assert report["can_write"] is False


@pytest.mark.unit
def test_the_spent_budget_is_visible(ledger):
    now = time.time()
    ledger.write_text(json.dumps(
        {"events": [now - 60] * 7, "blocked_until": 0}), encoding="utf-8")
    report = inspect_guardrails(Limits(max_per_day=10), path=ledger)
    assert report["used_today"] == 7
    assert report["remaining_today"] == 3


@pytest.mark.unit
def test_writes_older_than_a_day_do_not_count_against_today(ledger):
    now = time.time()
    ledger.write_text(json.dumps(
        {"events": [now - 25 * 3600] * 5, "blocked_until": 0}), encoding="utf-8")
    assert inspect_guardrails(Limits(), path=ledger)["used_today"] == 0


@pytest.mark.unit
def test_a_held_lock_is_reported_with_its_age_and_owner(ledger):
    lock = ledger.with_suffix(".lock")
    lock.write_text(json.dumps(
        {"pid": 4242, "started": time.time() - 90, "token": "abc"}), encoding="utf-8")
    report = inspect_guardrails(Limits(), path=ledger)
    assert report["lock"]["pid"] == 4242
    assert report["lock"]["age_h"] < 0.1
    assert report["lock"]["stale"] is False


@pytest.mark.unit
def test_an_abandoned_lock_is_called_stale_rather_than_left_to_puzzle_over(ledger):
    """One has been sitting in this repo's own state/ for nine hours.

    Age is the file's mtime, not the `started` field it carries: a live run
    refreshes the lock on every write, so "untouched this long" is the signal
    and "started this long ago" would steal the lock from a slow healthy run.
    """
    import os

    lock = ledger.with_suffix(".lock")
    lock.write_text(json.dumps(
        {"pid": 1, "started": time.time() - 9 * 3600, "token": "x"}), encoding="utf-8")
    stale = time.time() - 9 * 3600
    os.utime(lock, (stale, stale))
    report = inspect_guardrails(Limits(), path=ledger)
    assert report["lock"]["stale"] is True
    assert report["lock"]["age_h"] > 8


@pytest.mark.unit
def test_an_unreadable_lock_is_reported_not_swallowed(ledger):
    lock = ledger.with_suffix(".lock")
    lock.write_text("not json at all", encoding="utf-8")
    report = inspect_guardrails(Limits(), path=ledger)
    assert report["lock"] is not None
    assert report["lock"]["pid"] is None


@pytest.mark.unit
def test_reading_the_guardrails_never_writes(ledger):
    now = time.time()
    body = json.dumps({"events": [now - 25 * 3600] * 5, "blocked_until": 0})
    ledger.write_text(body, encoding="utf-8")
    inspect_guardrails(Limits(), path=ledger)
    assert ledger.read_text(encoding="utf-8") == body, \
        "status is documented as read-only; pruning must stay in memory"
