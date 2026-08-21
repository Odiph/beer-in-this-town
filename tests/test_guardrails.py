"""Tests for the anti-ban guardrails.

These matter more than the parsing tests: a parser bug produces wrong data, a
guardrail bug produces a banned account. Every gate is tested for failing
CLOSED — i.e. when uncertain, it stops.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from beer_in_this_town.guardrails import (
    STALE_LOCK_SECONDS,
    CircuitBreaker,
    Limits,
    RateLedger,
    Tripped,
    detect_block,
    ledger_lock,
    looks_signed_out,
)


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "rate_ledger.json"


@pytest.fixture
def limits():
    return Limits(max_per_run=10, max_per_day=20, cooloff_hours=6.0)


# --- rolling daily budget -------------------------------------------------
@pytest.mark.unit
def test_fresh_ledger_allows_a_start(limits, ledger_path):
    RateLedger(limits, ledger_path).assert_can_start()


@pytest.mark.unit
def test_writes_count_against_the_daily_budget(limits, ledger_path):
    ledger = RateLedger(limits, ledger_path)
    for _ in range(5):
        ledger.record_write()
    assert ledger.used_today() == 5
    assert ledger.remaining_today() == 15


@pytest.mark.unit
def test_budget_persists_across_restarts(limits, ledger_path):
    """The point of the ledger: re-running must not reset the allowance."""
    first = RateLedger(limits, ledger_path)
    for _ in range(20):
        first.record_write()

    second = RateLedger(limits, ledger_path)
    assert second.remaining_today() == 0
    with pytest.raises(Tripped, match="Daily write budget"):
        second.assert_can_start()


@pytest.mark.unit
def test_events_older_than_24h_stop_counting(limits, ledger_path):
    stale = time.time() - (25 * 3600)
    ledger_path.write_text(json.dumps({"events": [stale] * 20}), encoding="utf-8")
    ledger = RateLedger(limits, ledger_path)
    assert ledger.used_today() == 0
    ledger.assert_can_start()


@pytest.mark.unit
def test_run_budget_respects_every_ceiling(limits, ledger_path):
    ledger = RateLedger(limits, ledger_path)
    # per-run cap is 10, so an unbounded request is trimmed to 10
    assert ledger.budget_for_this_run(None) == 10
    # an explicit smaller request wins
    assert ledger.budget_for_this_run(3) == 3
    # and the daily remainder can cut it further
    for _ in range(14):
        ledger.record_write()
    assert ledger.budget_for_this_run(None) == 6


# --- cool-off -------------------------------------------------------------
@pytest.mark.unit
def test_cooloff_blocks_starting(limits, ledger_path):
    ledger = RateLedger(limits, ledger_path)
    ledger.start_cooloff("testing")
    with pytest.raises(Tripped, match="Cool-off active"):
        ledger.assert_can_start()


@pytest.mark.unit
def test_cooloff_survives_restart(limits, ledger_path):
    RateLedger(limits, ledger_path).start_cooloff("testing")
    with pytest.raises(Tripped, match="Cool-off active"):
        RateLedger(limits, ledger_path).assert_can_start()


@pytest.mark.unit
def test_expired_cooloff_allows_a_start(limits, ledger_path):
    ledger_path.write_text(
        json.dumps({"events": [], "blocked_until": time.time() - 60}),
        encoding="utf-8",
    )
    RateLedger(limits, ledger_path).assert_can_start()


@pytest.mark.unit
def test_corrupt_ledger_fails_closed(limits, ledger_path):
    """A corrupt ledger must NOT hand back a fresh allowance.

    The write happens after every save, so a truncated file is most likely
    precisely when a run died mid-flight -- e.g. right after a CAPTCHA. Handing
    that run a clean slate and no cool-off inverts the guardrail. Assume the
    worst instead.
    """
    ledger_path.write_text("{not json", encoding="utf-8")
    ledger = RateLedger(limits, ledger_path)

    assert ledger.corrupt is True
    assert ledger.remaining_today() == 0
    assert ledger.cooloff_remaining_s() > 0
    with pytest.raises(Tripped):
        ledger.assert_can_start()


@pytest.mark.unit
def test_ledger_write_is_atomic(limits, ledger_path):
    """No .tmp left behind, and the file is always complete JSON."""
    ledger = RateLedger(limits, ledger_path)
    ledger.record_write()
    assert not ledger_path.with_suffix(".tmp").exists()
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["events"]


# --- circuit breaker ------------------------------------------------------
@pytest.mark.unit
def test_breaker_trips_after_consecutive_failures():
    breaker = CircuitBreaker(limit=3)
    for _ in range(2):
        breaker.record_failure()
    assert not breaker.is_tripped
    breaker.record_failure()
    assert breaker.is_tripped


@pytest.mark.unit
def test_success_resets_the_breaker():
    breaker = CircuitBreaker(limit=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert not breaker.is_tripped


# --- block detection ------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Our systems have detected unusual traffic from your computer network",
        "Please complete the CAPTCHA to continue",
        "Verify it's you",
        "This page checks to see if it's really you sending the requests, not a robot",
        "Too many requests",
    ],
)
def test_interstitials_are_detected(text):
    assert detect_block(text) is not None


@pytest.mark.unit
def test_a_normal_page_is_not_flagged():
    assert detect_block("Brewerkz Riverside Point. 4.4 stars. Save. Directions.") is None


@pytest.mark.unit
def test_detection_is_case_insensitive():
    assert detect_block("UNUSUAL TRAFFIC DETECTED") is not None


@pytest.mark.unit
def test_signed_out_page_is_recognised():
    assert looks_signed_out("Sign in to Google Maps to see your places")


@pytest.mark.unit
def test_signed_in_page_is_not_mistaken_for_signed_out():
    """A signed-in page mentions Saved; a bare "Sign in" string is not enough."""
    assert not looks_signed_out("Saved lists. Sign in options. Your places.")


# --- cross-process exclusion ----------------------------------------------
# The ledger is the guardrail that "just run it again" cannot defeat. Two
# processes reading it at the same moment each saw the full remaining budget,
# and whichever wrote last silently discarded the other's events -- so the
# budget could be spent twice over and the file would not even show it.
def _second_run(path):
    """Stand in for another process trying to start while one is live."""
    with ledger_lock(path):
        pytest.fail("the second run must not be allowed to start")


@pytest.mark.unit
def test_lock_is_held_for_the_duration_of_a_run(ledger_path):
    with ledger_lock(ledger_path), pytest.raises(Tripped, match="already running"):
        _second_run(ledger_path)


@pytest.mark.unit
def test_lock_is_released_afterwards(ledger_path):
    with ledger_lock(ledger_path):
        pass
    with ledger_lock(ledger_path):
        pass  # a second run after the first finished is fine


@pytest.mark.unit
def test_lock_is_released_even_when_the_run_raises(ledger_path):
    with pytest.raises(RuntimeError), ledger_lock(ledger_path):
        raise RuntimeError("browser died mid-run")
    # A crashed run must not lock the user out of every future one.
    with ledger_lock(ledger_path):
        pass


@pytest.mark.unit
def test_a_stale_lock_is_broken_rather_than_blocking_forever(ledger_path):
    lock = ledger_path.with_suffix(".lock")
    lock.write_text('{"pid": 999999, "started": 0}', encoding="utf-8")
    old = time.time() - STALE_LOCK_SECONDS - 60
    os.utime(lock, (old, old))

    with ledger_lock(ledger_path):
        pass


@pytest.mark.unit
def test_a_fresh_lock_from_another_process_is_respected(ledger_path):
    lock = ledger_path.with_suffix(".lock")
    lock.write_text('{"pid": 999999, "started": 0}', encoding="utf-8")

    with pytest.raises(Tripped, match="already running"), ledger_lock(ledger_path):
        pytest.fail("a live lock must fail closed")
