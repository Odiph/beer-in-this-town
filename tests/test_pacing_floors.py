"""Pacing is a guardrail, so it cannot be a suggestion.

AGENTS.md rule 4 says "do not lower the pacing -- raise them if throttled, do
not lower them", and nothing enforced it: `--min-gap 0 --max-gap 0` was
accepted, `--delay 0.05` was accepted, and `--delay 0` was ignored only
because 0 is falsy. Every other guardrail in this project fails closed; these
were prose.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.cli import build_parser, check_pacing
from beer_in_this_town.notes import MIN_GAP_S as NOTES_MIN
from beer_in_this_town.pin_to_list import MIN_GAP_S as PIN_MIN


def _parse(argv):
    return build_parser().parse_args(argv)


@pytest.mark.unit
@pytest.mark.parametrize("cmd,floor", [("pin", PIN_MIN), ("notes", NOTES_MIN)])
def test_write_pacing_cannot_be_lowered_below_the_default(cmd, floor):
    with pytest.raises(SystemExit):
        _parse([cmd, "--csv", "x.csv", "--min-gap", "0"])
    with pytest.raises(SystemExit):
        _parse([cmd, "--csv", "x.csv", "--min-gap", str(floor / 2)])


@pytest.mark.unit
@pytest.mark.parametrize("cmd", ["pin", "notes"])
def test_write_pacing_can_still_be_raised(cmd):
    """Throttled users are told to raise these; that must keep working."""
    args = _parse([cmd, "--csv", "x.csv", "--min-gap", "60", "--max-gap", "120"])
    assert args.min_gap == 60 and args.max_gap == 120


@pytest.mark.unit
def test_read_pacing_cannot_be_lowered_below_the_default():
    with pytest.raises(SystemExit):
        _parse(["run", "--delay", "0.05"])


@pytest.mark.unit
def test_a_zero_delay_is_rejected_rather_than_quietly_ignored():
    """`--delay 0` was ignored because 0 is falsy -- it looked accepted."""
    with pytest.raises(SystemExit):
        _parse(["run", "--delay", "0"])


@pytest.mark.unit
def test_an_inverted_gap_range_is_refused():
    """--min-gap 30 --max-gap 1 was accepted and paced on nonsense."""
    with pytest.raises(ValueError, match="max-gap"):
        check_pacing(min_gap=30.0, max_gap=1.0)


@pytest.mark.unit
def test_a_sane_range_passes():
    assert check_pacing(min_gap=8.0, max_gap=16.0) is None


# --- protections that survive a restart -----------------------------------
@pytest.mark.unit
def test_the_hourly_read_ceiling_survives_a_restart(tmp_path, monkeypatch):
    """The README calls 600/hour a cap; it lived in memory.

    Restarting the process handed back a fresh allowance, so the one number
    the docs describe as a hard ceiling was the one an ordinary retry loop
    could reset -- unlike the write ledger, whose whole point is persistence.
    """
    from beer_in_this_town import http_client
    from beer_in_this_town.config import Settings

    path = tmp_path / "read_budget.json"
    monkeypatch.setattr(http_client, "READ_BUDGET", path)
    s = Settings(hourly_budget=3)

    first = http_client.ReadBudget(s, path=path)
    for _ in range(3):
        first.record()
    assert first.remaining() == 0

    restarted = http_client.ReadBudget(s, path=path)
    assert restarted.remaining() == 0, "a restart must not refill the ceiling"


@pytest.mark.unit
def test_the_read_ceiling_rolls_forward_after_an_hour(tmp_path, monkeypatch):
    import time as _time

    from beer_in_this_town import http_client
    from beer_in_this_town.config import Settings

    path = tmp_path / "read_budget.json"
    s = Settings(hourly_budget=2)
    budget = http_client.ReadBudget(s, path=path)
    budget.record()
    budget.record()
    assert budget.remaining() == 0

    # Capture the real clock first: `_time` is the same module object the
    # patch lands on, so a lambda calling _time.time() would call itself.
    real_now = _time.time()
    monkeypatch.setattr(http_client.time, "time", lambda: real_now + 3700)
    assert http_client.ReadBudget(s, path=path).remaining() == 2


@pytest.mark.unit
def test_the_circuit_breaker_survives_a_restart(tmp_path):
    """Three failures on the last three places tripped nothing.

    is_tripped was only read at the top of the next iteration, and the count
    started at zero every run -- so a run that failed its way to the end set
    no cool-off and the next one began with a clean slate.
    """
    from beer_in_this_town.guardrails import CircuitBreaker

    path = tmp_path / "breaker.json"
    breaker = CircuitBreaker(limit=3, path=path)
    for _ in range(2):
        breaker.record_failure()
    assert not breaker.is_tripped

    resumed = CircuitBreaker(limit=3, path=path)
    assert resumed.consecutive == 2, "a restart must not clear the run of failures"
    resumed.record_failure()
    assert resumed.is_tripped


@pytest.mark.unit
def test_a_success_clears_the_persisted_breaker(tmp_path):
    from beer_in_this_town.guardrails import CircuitBreaker

    path = tmp_path / "breaker.json"
    breaker = CircuitBreaker(limit=3, path=path)
    breaker.record_failure()
    breaker.record_success()
    assert CircuitBreaker(limit=3, path=path).consecutive == 0


@pytest.mark.unit
def test_a_tripped_breaker_does_not_lock_the_account_out_forever(tmp_path):
    """Persisting the count created a self-perpetuating lockout.

    `is_tripped` is read at the top of the loop, before any place is
    attempted, and `record_success` -- the only thing that cleared it -- sits
    after a successful write that can therefore never happen. So a stale 3
    tripped every subsequent run instantly, started a fresh six-hour cool-off
    each time, and never came down.

    The cool-off IS the punishment for tripping. Starting one settles the
    debt, so the count resets with it.
    """
    from beer_in_this_town.guardrails import CircuitBreaker

    path = tmp_path / "breaker.json"
    breaker = CircuitBreaker(limit=3, path=path)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.is_tripped

    breaker.reset()
    assert CircuitBreaker(limit=3, path=path).is_tripped is False


@pytest.mark.unit
def test_a_stale_breaker_count_expires(tmp_path):
    """Failures from days ago say nothing about conditions now."""
    import json
    import time as _t

    from beer_in_this_town.guardrails import CircuitBreaker

    path = tmp_path / "breaker.json"
    path.write_text(json.dumps({"consecutive": 3, "at": _t.time() - 48 * 3600}),
                    encoding="utf-8")
    assert CircuitBreaker(limit=3, path=path).consecutive == 0


@pytest.mark.unit
def test_a_recent_breaker_count_is_kept(tmp_path):
    """Within the window it still carries across a restart, which was the point."""
    import json
    import time as _t

    from beer_in_this_town.guardrails import CircuitBreaker

    path = tmp_path / "breaker.json"
    path.write_text(json.dumps({"consecutive": 2, "at": _t.time() - 60}),
                    encoding="utf-8")
    assert CircuitBreaker(limit=3, path=path).consecutive == 2


@pytest.mark.unit
def test_status_can_see_the_breaker(tmp_path):
    """status said can_write: true while the next run tripped instantly."""
    import json
    import time as _t

    from beer_in_this_town.guardrails import Limits, inspect_guardrails

    ledger = tmp_path / "rate_ledger.json"
    (tmp_path / "breaker.json").write_text(
        json.dumps({"consecutive": 3, "at": _t.time()}), encoding="utf-8")
    report = inspect_guardrails(Limits(), path=ledger,
                                breaker_path=tmp_path / "breaker.json")
    assert report["breaker"]["consecutive"] == 3
    assert report["breaker"]["tripped"] is True
    assert report["can_write"] is False


@pytest.mark.unit
def test_a_damaged_read_budget_fails_closed(tmp_path):
    """Reading a truncated budget as "nothing spent" refills the ceiling.

    The write ledger fails closed; this was modelled on it and failed open,
    which hands back a full hour's allowance for the price of one interrupted
    write -- and the file is rewritten up to 600 times an hour.
    """
    from beer_in_this_town import http_client
    from beer_in_this_town.config import Settings

    path = tmp_path / "read_budget.json"
    path.write_text('{"window_start": 123', encoding="utf-8")   # truncated
    s = Settings(hourly_budget=600)
    assert http_client.ReadBudget(s, path=path).remaining() == 0


@pytest.mark.unit
def test_the_read_budget_is_written_atomically(tmp_path):
    from beer_in_this_town import http_client
    from beer_in_this_town.config import Settings

    path = tmp_path / "read_budget.json"
    budget = http_client.ReadBudget(Settings(hourly_budget=5), path=path)
    budget.record()
    assert not path.with_suffix(".tmp").exists(), "no temp file left behind"
    assert http_client.ReadBudget(Settings(hourly_budget=5), path=path).remaining() == 4
