"""Write the Untappd stats into each saved place's note.

A Google Maps saved list carries no data of its own — it is just pins. The one
place per-venue information can live is the free-text **note** attached to a
saved place, which shows in the list view and on the place card. Without it the
saved list is strictly less informative than the My Maps layer, which renders
the stats in each pin's info window.

So this is a second pass: for every place already in the list, type a compact
line of Untappd stats into its note.

Same discipline as `pin_to_list`:
  * every note is verified by reloading and reading it back;
  * notes count against the rate ledger — editing a note is a write;
  * progress is journalled separately, so the pass is resumable and never
    rewrites a note that is already correct.
"""
from __future__ import annotations

import csv
import json
import logging
import random
import time
from pathlib import Path

from .config import STATE_DIR, Settings
from .guardrails import CircuitBreaker, Limits, RateLedger, Tripped

log = logging.getLogger(__name__)

NOTE_JOURNAL = STATE_DIR / "noted.json"

NOTE_FIELD = (
    "textarea[placeholder*='Add a note'], "
    "textarea[aria-label*='note'], "
    "input[placeholder*='Add a note']"
)


def format_note(row: dict[str, str]) -> str:
    """Compact, human-readable, and stable across runs.

    Stability matters: the pass compares the existing note to this string to
    decide whether to rewrite, so any incidental variation would cause every
    note to be rewritten on every run — pointless writes against a rate limit.
    """
    def num(key: str) -> str | None:
        raw = (row.get(key) or "").strip().replace(",", "")
        if not raw.isdigit():
            return None
        return f"{int(raw):,}"

    total, unique, monthly = num("total"), num("unique"), num("monthly")

    # A rank on its own ("Untappd #2") carries no information about the venue
    # and would still cost a write against the rate budget. Require a stat.
    if total is None and unique is None and monthly is None:
        return ""

    bits = []
    rank = (row.get("rank") or "").strip()
    if rank:
        bits.append(f"Untappd #{rank}")

    if total:
        bits.append(f"{total} check-ins")
    if unique:
        bits.append(f"{unique} unique")
    if monthly is not None:
        bits.append(f"{monthly}/month")
    # ASCII separator on purpose: a middot survives Maps fine but
    # mangles in Windows console logs, and the note is compared as a
    # string to decide whether to rewrite.
    return " | ".join(bits)


def notes_from_csv(path: Path) -> list[tuple[str, str | None, str]]:
    """(name, address, note) for every row that has something worth saying."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    out: list[tuple[str, str | None, str]] = []
    for row in rows:
        name = (row.get("name") or row.get("Name") or "").strip()
        if not name:
            continue
        address = (row.get("address") or row.get("Address") or "").strip() or None
        city = (row.get("city") or "").strip()
        if address and city and city.lower() not in address.lower():
            address = f"{address}, {city}"
        note = format_note(row)
        if note:
            out.append((name, address, note))
    return out


def _load_journal() -> dict[str, str]:
    if NOTE_JOURNAL.exists():
        return json.loads(NOTE_JOURNAL.read_text(encoding="utf-8"))
    return {}


def _save_journal(journal: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    NOTE_JOURNAL.write_text(
        json.dumps(journal, indent=1, ensure_ascii=False), encoding="utf-8"
    )


def _read_note(page) -> str | None:
    """Current note text, or None if the field cannot be read."""
    try:
        field = page.locator(NOTE_FIELD).first
        if field.count() == 0:
            return None
        return (field.input_value(timeout=5000) or "").strip()
    except Exception:
        return None


def _write_note(page, text: str) -> str | None:
    """Type the note, commit it, and read it back after a reload."""
    place_url = page.url

    field = page.locator(NOTE_FIELD).first
    field.click(timeout=15_000)
    field.fill(text, timeout=15_000)

    # Maps commits the note on blur. Tab is the least destructive way to blur;
    # Escape can discard the edit.
    page.keyboard.press("Tab")
    page.wait_for_timeout(3000)

    page.goto(place_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3500)
    return _read_note(page)


def add_notes(
    places: list[tuple[str, str | None, str]],
    s: Settings,
    list_name: str,
    *,
    limit: int | None = None,
    headless: bool = False,
    region: str | None = None,
    min_gap_s: float = 5.0,
    max_gap_s: float = 11.0,
    limits: Limits | None = None,
) -> dict[str, str]:
    """Add a stats note to each place already saved in `list_name`."""
    from playwright.sync_api import sync_playwright

    from .pin_to_list import (
        _abort_if_blocked,
        _open_place,
        _place_heading,
        _saved_in,
        journal_key,
        place_matches,
    )

    limits = limits or Limits()
    ledger = RateLedger(limits)
    breaker = CircuitBreaker(limits.max_consecutive_failures)
    ledger.assert_can_start()

    journal = _load_journal()
    todo = [
        (n, a, note) for (n, a, note) in places
        if journal.get(journal_key(n, a)) != "ok"
    ]

    allowed = ledger.budget_for_this_run(limit)
    if len(todo) > allowed:
        log.warning("Trimming to %d note(s): per-run cap %d, %d left today.",
                    allowed, limits.max_per_run, ledger.remaining_today())
        todo = todo[:allowed]

    log.info("%d note(s) to write (%d already done, %d writes left today)",
             len(todo), sum(1 for v in journal.values() if v == "ok"),
             ledger.remaining_today())
    if not todo:
        return journal

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(s.profile_dir),
            channel="chrome",
            headless=headless,
            viewport={"width": 1400, "height": 950},
            locale="en-US",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            for i, (name, address, note) in enumerate(todo, 1):
                key = journal_key(name, address)
                log.info("[%3d/%d] %s", i, len(todo), name)

                _abort_if_blocked(page, ledger)
                if breaker.is_tripped:
                    ledger.start_cooloff(
                        f"{breaker.consecutive} consecutive failures"
                    )
                    raise Tripped(
                        f"Stopped after {breaker.consecutive} consecutive "
                        "failures. Cool-off set; progress is journalled."
                    )
                if ledger.remaining_today() <= 0:
                    log.warning("Daily write budget reached -- stopping cleanly.")
                    break
                if i > 1 and (i - 1) % limits.break_every == 0:
                    log.info("  taking a %.0fs break", limits.break_seconds)
                    time.sleep(limits.break_seconds)

                if not _open_place(page, name, address, region):
                    journal[key] = "not-found"
                    log.warning("  no Google Maps result -- skipped")
                    _save_journal(journal)
                    breaker.record_failure()
                    time.sleep(random.uniform(min_gap_s, max_gap_s))
                    continue

                heading = _place_heading(page)
                if heading and not place_matches(name, heading):
                    journal[key] = "ambiguous"
                    log.warning("  Maps opened %r -- skipped", heading)
                    _save_journal(journal)
                    time.sleep(random.uniform(min_gap_s, max_gap_s))
                    continue

                # Only annotate places that are actually in the target list;
                # otherwise the note field may not even be present.
                saved_in = _saved_in(page)
                if saved_in is None or list_name.lower() not in saved_in.lower():
                    journal[key] = "not-in-list"
                    log.warning("  not in %s -- pin it first", list_name)
                    _save_journal(journal)
                    time.sleep(random.uniform(min_gap_s, max_gap_s))
                    continue

                existing = _read_note(page)
                if existing == note:
                    journal[key] = "ok"
                    log.info("  note already correct")
                    _save_journal(journal)
                    continue

                ledger.record_write()
                try:
                    written = _write_note(page, note)
                except Exception as exc:
                    log.warning("  could not write note: %s", exc)
                    _abort_if_blocked(page, ledger)
                    written = None

                if written == note:
                    journal[key] = "ok"
                    breaker.record_success()
                    log.info("  note written: %s", note)
                else:
                    journal[key] = "failed"
                    breaker.record_failure()
                    log.error("  note did not stick (reads %r)", written)

                _save_journal(journal)
                time.sleep(random.uniform(min_gap_s, max_gap_s))
        finally:
            try:
                ctx.close()
            except Exception as exc:
                log.debug("Browser teardown was already done: %s", exc)

    ok = sum(1 for v in journal.values() if v == "ok")
    log.info("Done. %d note(s) written or already correct.", ok)
    return journal
