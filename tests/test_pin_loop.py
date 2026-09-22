"""The write loop itself, exercised offline.

`pin_to_list`'s pure helpers have their own tests, and `score_labels` has its
own. What had never run was the loop that ties them together -- the one that
charges the rate ledger, writes the journal, feeds the circuit breaker and
decides whether a place was saved. That is the code where being wrong costs
somebody's Google account rather than a CSV row, and it was the least covered
code in the repo.

Nothing here touches Playwright, the network or an account: the browser
boundary is stubbed and the page-touching helpers are replaced, so what runs
is the orchestration and nothing else.
"""
from __future__ import annotations

import sys
import types

import pytest

from beer_in_this_town import pin_to_list
from beer_in_this_town.config import Settings
from beer_in_this_town.guardrails import Limits, Tripped
from beer_in_this_town.pin_to_list import AmbiguousList, pin_places

PLACES = [("Ghost Whale", "24 Clapham High St"),
          ("The Kernel", "11 Dockley Rd"),
          ("Mother Kelly's", "251 Paradise Row")]


class _FakePage:
    """Just enough surface for the loop's own calls, which never inspect it."""

    url = "https://maps.google.com/place"

    def __init__(self):
        self.keyboard = types.SimpleNamespace(press=lambda *a, **kw: None)

    def __getattr__(self, _name):
        return lambda *a, **kw: None


class _FakeContext:
    def __init__(self):
        self.pages = [_FakePage()]
        self.closed = False

    def new_page(self):
        return self.pages[0]

    def close(self):
        self.closed = True


@pytest.fixture
def browser(monkeypatch):
    """Stub the Playwright boundary so the loop runs with no browser."""
    ctx = _FakeContext()

    class _PW:
        chromium = types.SimpleNamespace(
            launch_persistent_context=lambda **kw: ctx)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: _PW()
    module.TimeoutError = type("TimeoutError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return ctx


@pytest.fixture
def loop(monkeypatch, browser):
    """Replace every page-touching helper; keep the orchestration real."""
    calls: dict[str, list] = {"unsaved": [], "opened": []}

    def configure(*, pin_once, found=True, saved_in="", heading=None):
        monkeypatch.setattr(pin_to_list, "_assert_ready", lambda p, ln: None)
        monkeypatch.setattr(pin_to_list, "_abort_if_blocked", lambda p, led: None)
        monkeypatch.setattr(pin_to_list, "_saved_in", lambda p: saved_in)
        monkeypatch.setattr(pin_to_list, "_place_heading",
                            lambda p: heading if heading else "Ghost Whale")
        monkeypatch.setattr(pin_to_list, "place_matches", lambda a, b, **kw: True)
        monkeypatch.setattr(pin_to_list, "_unsave",
                            lambda p, wrong: calls["unsaved"].append(wrong))

        def _open(page, name, address, region):
            calls["opened"].append(name)
            return found

        monkeypatch.setattr(pin_to_list, "_open_place", _open)
        monkeypatch.setattr(pin_to_list, "_pin_once", pin_once)
        return calls

    monkeypatch.setattr(pin_to_list.time, "sleep", lambda _: None)
    monkeypatch.setattr(pin_to_list.random, "uniform", lambda a, b: 0)
    return configure


def _run(tmp_path, *, breaker_limit=3, **kw):
    limits = Limits(max_per_run=60, max_per_day=100,
                    max_consecutive_failures=breaker_limit)
    return pin_places(PLACES, Settings(), "London Bars", limits=limits,
                      min_gap_s=0, max_gap_s=0, **kw)


# --- the happy path, so the rest means something ---------------------------
@pytest.mark.unit
def test_a_verified_save_is_journalled_ok(tmp_path, loop):
    loop(pin_once=lambda p, ln: "London Bars")
    journal = _run(tmp_path)
    assert list(journal.values()) == ["ok", "ok", "ok"]


@pytest.mark.unit
def test_each_saved_place_costs_exactly_one_write(tmp_path, loop, monkeypatch):
    """The budget is charged before the interaction, once per attempt."""
    spent = []
    monkeypatch.setattr(pin_to_list.RateLedger, "record_write",
                        lambda self: spent.append(1))
    loop(pin_once=lambda p, ln: "London Bars")
    _run(tmp_path)
    assert len(spent) == len(PLACES)


# --- the wrong-list failure this module exists to prevent ------------------
@pytest.mark.unit
def test_a_save_that_lands_in_a_similarly_named_list_is_not_accepted(
    tmp_path, loop
):
    """"Bars" is a substring of "London Bars", and that used to verify as ok."""
    calls = loop(pin_once=lambda p, ln: "Bars")
    # Breaker out of the way: this asserts the verification, not the trip.
    journal = _run(tmp_path, breaker_limit=99)
    assert set(journal.values()) == {"failed"}, journal
    assert calls["unsaved"], "a save that landed elsewhere must be undone"


@pytest.mark.unit
def test_an_unverifiable_save_is_never_assumed_either_way(tmp_path, loop):
    """_saved_in returns None when it could not tell. Guessing 'not saved'
    makes the next attempt click a checked row, which un-saves it."""
    loop(pin_once=lambda p, ln: None)
    journal = _run(tmp_path, breaker_limit=99)
    assert set(journal.values()) == {"failed"}


# --- ambiguity must abort before it spends anything ------------------------
@pytest.mark.unit
def test_an_ambiguous_list_aborts_the_run_rather_than_retrying(tmp_path, loop):
    """It was swallowed by the per-attempt `except Exception`, after the
    budget had been charged -- so an ambiguous name cost three writes per
    place, journalled `failed`, fed the breaker, and `list_ambiguous` could
    never reach the envelope."""
    def ambiguous(page, list_name):
        raise AmbiguousList("two lists named 'London Bars'")

    loop(pin_once=ambiguous)
    with pytest.raises(AmbiguousList):
        _run(tmp_path)


@pytest.mark.unit
def test_an_ambiguous_list_costs_one_write_not_nine(tmp_path, loop, monkeypatch):
    """Three places x three attempts was the old cost of a name that could
    never work."""
    spent = []
    monkeypatch.setattr(pin_to_list.RateLedger, "record_write",
                        lambda self: spent.append(1))

    def ambiguous(page, list_name):
        raise AmbiguousList("two lists named 'London Bars'")

    loop(pin_once=ambiguous)
    with pytest.raises(AmbiguousList):
        _run(tmp_path)
    assert len(spent) == 1, f"spent {len(spent)} writes on an impossible name"


# --- the breaker, end to end ----------------------------------------------
@pytest.mark.unit
def test_consecutive_failures_trip_the_breaker_and_start_a_cooloff(
    tmp_path, loop, monkeypatch
):
    started: list[str] = []
    monkeypatch.setattr(pin_to_list.RateLedger, "start_cooloff",
                        lambda self, why: started.append(why))

    def always_fails(page, list_name):
        raise RuntimeError("the picker never opened")

    loop(pin_once=always_fails)
    with pytest.raises(Tripped):
        _run(tmp_path)
    assert started, "a trip must start a cool-off"


@pytest.mark.unit
def test_the_breaker_is_cleared_when_the_cooloff_starts(
    tmp_path, loop, monkeypatch
):
    """Persisting the count without this was a permanent lockout: the trip
    fires before any place is attempted, so no success can ever clear it."""
    monkeypatch.setattr(pin_to_list.RateLedger, "start_cooloff",
                        lambda self, why: None)

    def always_fails(page, list_name):
        raise RuntimeError("the picker never opened")

    loop(pin_once=always_fails)
    with pytest.raises(Tripped):
        _run(tmp_path)

    from beer_in_this_town.guardrails import CircuitBreaker
    assert CircuitBreaker(limit=3).consecutive == 0, \
        "a tripped breaker must not lock the next run out before it starts"


# --- resumability ----------------------------------------------------------
@pytest.mark.unit
def test_places_already_done_are_not_re_saved(tmp_path, loop):
    loop(pin_once=lambda p, ln: "London Bars")
    _run(tmp_path)
    calls = loop(pin_once=lambda p, ln: "London Bars")
    calls["opened"].clear()          # the fixture's list spans both runs
    _run(tmp_path)
    assert calls["opened"] == [], "a resumed run must skip completed places"


@pytest.mark.unit
def test_a_place_maps_cannot_find_is_recorded_and_not_retried_forever(
    tmp_path, loop
):
    loop(pin_once=lambda p, ln: "London Bars", found=False)
    journal = _run(tmp_path)
    assert set(journal.values()) == {"not-found"}


@pytest.mark.unit
def test_the_run_is_trimmed_to_the_remaining_budget(tmp_path, loop, monkeypatch):
    """Trimming is correct behaviour, not a failure -- but it has to happen."""
    loop(pin_once=lambda p, ln: "London Bars")
    journal = pin_places(PLACES, Settings(), "London Bars",
                         limits=Limits(max_per_run=2, max_per_day=100),
                         min_gap_s=0, max_gap_s=0)
    assert sum(1 for v in journal.values() if v == "ok") == 2


# --- a person's own saves are never touched --------------------------------
@pytest.mark.unit
def test_a_place_already_in_another_list_is_saved_and_that_list_left_alone(
    tmp_path, loop
):
    """Found live: The Rake was already in the person's "London MTP25". The
    save worked and the panel read "London Bars Test & London MTP25"; the
    tool called it a wrong-list save and tried to "undo" the joined text.
    It must read the join, count the save, and never unsave a list the
    place was in before the attempt."""
    calls = loop(saved_in="London MTP25",
                 pin_once=lambda p, ln: "London Bars & London MTP25")
    journal = _run(tmp_path)
    assert set(journal.values()) == {"ok"}
    assert calls["unsaved"] == []


@pytest.mark.unit
def test_only_the_list_this_attempt_added_is_undone(tmp_path, loop):
    calls = loop(saved_in="London MTP25",
                 pin_once=lambda p, ln: "London MTP25 & Bars")
    _run(tmp_path, breaker_limit=99)
    assert set(calls["unsaved"]) == {"Bars"}, calls["unsaved"]


# --- reading the result of a write -----------------------------------------
@pytest.mark.unit
def test_a_save_is_not_called_lost_because_the_line_rendered_late(monkeypatch):
    """Live, with a fixed 1.5 s wait, most places read "not saved" on attempt
    1 and stuck on 2 or 3 -- ~2.5 writes each against a 100-a-day budget."""
    answers = iter(["", "", "London Bars Test"])
    monkeypatch.setattr(pin_to_list, "_saved_in", lambda p: next(answers))
    got = pin_to_list._saved_in_settled(types.SimpleNamespace(), timeout_s=10,
                                        sleep=lambda s: None)
    assert got == "London Bars Test"


@pytest.mark.unit
def test_a_place_that_really_is_unsaved_still_answers_empty(monkeypatch):
    monkeypatch.setattr(pin_to_list, "_saved_in", lambda p: "")
    assert pin_to_list._saved_in_settled(types.SimpleNamespace(), timeout_s=0.1,
                                         sleep=lambda s: None) == ""


@pytest.mark.unit
def test_an_unreadable_panel_still_answers_unknown(monkeypatch):
    monkeypatch.setattr(pin_to_list, "_saved_in", lambda p: None)
    assert pin_to_list._saved_in_settled(types.SimpleNamespace(), timeout_s=0.1,
                                         sleep=lambda s: None) is None
