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
import re
import time
from datetime import date
from pathlib import Path

from .config import STATE_DIR, Settings, scope_slug
from .guardrails import (
    CircuitBreaker,
    Limits,
    RateLedger,
    Tripped,
    single_writer,
)

log = logging.getLogger(__name__)

# Pre-scoping layout: one journal for every list on the machine, so a
# place saved into one list counted as done for all of them.
LEGACY_NOTE_JOURNAL = STATE_DIR / "noted.json"


def journal_path(list_name: str) -> Path:
    """A journal records what was written to *one* saved list."""
    return STATE_DIR / f"noted_{scope_slug(list_name)}.json"

# Pacing between notes. Editing a note is a lighter write than creating a
# save, so this sits between the scraper and `pin`. As in pin_to_list,
# these are the single source: the CLI defaults derive from them.
MIN_GAP_S = 5.0
MAX_GAP_S = 11.0

# Found live 2026-09-24: the note is no longer on the place page. It sits
# under the "Saved in" row, which starts collapsed, with one box per list the
# place is saved in, each in a block reading "Saved in <list> Private · 33
# places / Add a note". The right box is the one whose block names the list:
# the first box may be another list's -- a shared one, whose note everyone on
# it can read.
NOTE_FIELD = (
    "textarea[aria-label='Add note'], "
    "textarea[aria-label*='note'], "
    "textarea[placeholder*='Add a note']"
)
# The folded "Saved in" row is itself a button (aria-expanded="false").
# Unfolded, it is replaced by a different button ("Hide place lists
# details"), so this matches only while folded -- and a reload folds it
# again, which is why the read-back after a write must unfold it too.
LISTS_TOGGLE = "button[aria-expanded='false']:has-text('Saved in')"

_NOTE_BLOCK = re.compile(
    # \s*, not \s+: the live innerText is "Saved inLondon Bars Test" -- the
    # label and the list's link are adjacent inline elements.
    r"Saved in\s*(?P<name>.+?)\s+(?:Private|Shared|Public)\s*·", re.S)

# For each note box: the text of the nearest ancestor that says "Saved in"
# and holds no other box -- that box's own list block.
_BLOCKS_JS = """sel => [...document.querySelectorAll(sel)].map(box => {
  let node = box;
  for (let i = 0; i < 10 && node.parentElement; i++) {
    const up = node.parentElement;
    if (up.querySelectorAll(sel).length > 1) break;
    node = up;
    if ((node.innerText || '').includes('Saved in')) return node.innerText;
  }
  return '';
})"""


class NoteBoxMissing(Exception):
    """The place shows no note box for the target list. Nothing was typed."""


def note_block_list(block: str) -> str | None:
    """The list a note box belongs to, from its block's text."""
    m = _NOTE_BLOCK.search(block or "")
    return " ".join(m.group("name").split()) if m else None


def pick_note_box(blocks: list[str], list_name: str) -> int | None:
    """The index of the target list's box: exactly one, or None.

    Exact name, as `saved_in_target` compares them: "London Bars" is not
    "London Bars Test". Two boxes claiming the list is ambiguous, not a pick.
    """
    from .pin_to_list import _list_key

    want = _list_key(list_name)
    hits = [i for i, b in enumerate(blocks)
            if _list_key(note_block_list(b) or "") == want]
    return hits[0] if len(hits) == 1 else None


DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")


def data_date(path: Path) -> str:
    """When the DATA was captured -- not today.

    This is deliberate. If the note said "as of <today>" it would differ from
    the stored note on every run, so every note would be rewritten daily
    against a rate-limited budget while saying nothing new. Dating the data
    instead means a note changes only when the underlying numbers do.

    Taken from the filename stamp our own exports carry, falling back to the
    file's modification time.
    """
    match = DATE_IN_NAME.search(path.name)
    if match:
        return match.group(1)
    return date.fromtimestamp(path.stat().st_mtime).isoformat()


def format_note(row: dict[str, str], as_of: str | None = None) -> str:
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
    if as_of:
        bits.append(f"as of {as_of}")
    # ASCII separator on purpose: a middot survives Maps fine but
    # mangles in Windows console logs, and the note is compared as a
    # string to decide whether to rewrite.
    return " | ".join(bits)


def notes_from_csv(path: Path) -> list[tuple[str, str | None, str]]:
    """(name, address, note) for every row that has something worth saying."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    as_of = data_date(path)
    out: list[tuple[str, str | None, str]] = []
    for row in rows:
        name = (row.get("name") or row.get("Name") or "").strip()
        if not name:
            continue
        address = (row.get("address") or row.get("Address") or "").strip() or None
        city = (row.get("city") or "").strip()
        if address and city and city.lower() not in address.lower():
            address = f"{address}, {city}"
        note = format_note(row, as_of)
        if note:
            out.append((name, address, note))
    return out


def _load_journal(list_name: str) -> dict[str, str]:
    path = journal_path(list_name)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if LEGACY_NOTE_JOURNAL.exists():
        # The pre-scoping journal does not record which list it was built
        # for. Adopting it was a guess, and found live 2026-09-24 it guessed
        # wrong: a Singapore noted.json became the journal of "London Bars
        # Test". Never guess; say how to adopt it. (pin_to_list does the same.)
        log.warning(
            "state/noted.json predates per-list journals and names no list, "
            "so it is not used for %r. If it belongs to that list, rename it "
            "to %s.", list_name, path.name,
        )
    return {}


def _save_journal(journal: dict[str, str], list_name: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    journal_path(list_name).write_text(
        json.dumps(journal, indent=1, ensure_ascii=False), encoding="utf-8"
    )


def note_action(existing: str | None, note: str) -> str:
    """"ok" (already right), "write", or "no-box" (not this list's to write).

    "no-box" spends no write and types nothing: an unidentified box may be
    another list's, and the budget is for writes that can land.
    """
    if existing is None:
        return "no-box"
    return "ok" if existing == note else "write"


def _open_lists(page) -> None:
    """Unfold the "Saved in" row, where the note boxes are. Never fold it."""
    toggle = page.locator(LISTS_TOGGLE).first
    if toggle.count():
        toggle.click(timeout=10_000)
        page.wait_for_timeout(1500)


def _note_box(page, list_name: str):
    """The target list's note box, or None when there is not exactly one."""
    _open_lists(page)
    blocks = page.evaluate(_BLOCKS_JS, NOTE_FIELD) or []
    i = pick_note_box(blocks, list_name)
    if i is None:
        log.warning("  no note box for %r among: %s", list_name,
                    [note_block_list(b) for b in blocks])
        return None
    return page.locator(NOTE_FIELD).nth(i)


def _read_note(page, list_name: str) -> str | None:
    """The target list's note text, or None if its box cannot be read.

    None is "unknown", never "empty": empty means "write it", and writing
    into a box we could not identify is how a note lands in another list.
    """
    try:
        box = _note_box(page, list_name)
        if box is None:
            return None
        return (box.input_value(timeout=5000) or "").strip()
    except Exception as exc:
        # Unknown is still the answer, but say why: swallowed silently, a
        # read-back failure looked like a write failure (found live).
        log.warning("  could not read the note for %r: %s", list_name, exc)
        return None


def _write_note(page, text: str, list_name: str) -> str | None:
    """Type the note into the list's own box, commit it, read it back."""
    place_url = page.url

    field = _note_box(page, list_name)
    if field is None:
        raise NoteBoxMissing(
            f"no note box for {list_name!r} on this place; nothing typed")
    field.click(timeout=15_000)
    field.fill(text, timeout=15_000)

    # Maps commits the note on blur. Tab is the least destructive way to blur;
    # Escape can discard the edit.
    page.keyboard.press("Tab")
    page.wait_for_timeout(3000)

    page.goto(place_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3500)
    return _read_note(page, list_name)


@single_writer
def add_notes(
    places: list[tuple[str, str | None, str]],
    s: Settings,
    list_name: str,
    *,
    limit: int | None = None,
    headless: bool = False,
    region: str | None = None,
    min_gap_s: float = MIN_GAP_S,
    max_gap_s: float = MAX_GAP_S,
    limits: Limits | None = None,
) -> dict[str, str]:
    """Add a stats note to each place already saved in `list_name`."""
    from playwright.sync_api import sync_playwright

    from .pin_to_list import (
        _abort_if_blocked,
        _assert_ready,
        _open_place,
        _place_heading,
        _saved_in,
        journal_key,
        place_matches,
        saved_in_target,
    )

    limits = limits or Limits()
    ledger = RateLedger(limits)
    breaker = CircuitBreaker(limits.max_consecutive_failures)
    ledger.assert_can_start()

    journal = _load_journal(list_name)
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
            # The same pre-flight `pin` runs, and for the same reason:
            # signed-out Google Maps loads perfectly happily. Without it a
            # signed-out profile read every place as "not in the list",
            # journalled a hundred rows `not-in-list`, reported ok: true and
            # told the user to run pin first -- a wrong diagnosis reached by
            # loading a hundred pages while signed out. The other outcome was
            # worse: the block detector recognised the signed-out page and
            # started a six-hour cool-off, which also blocks `pin`, for a
            # condition `pin` itself reports as not_signed_in with no cool-off.
            _assert_ready(page, list_name)

            for i, (name, address, note) in enumerate(todo, 1):
                key = journal_key(name, address)
                log.info("[%3d/%d] %s", i, len(todo), name)

                _abort_if_blocked(page, ledger)
                if breaker.is_tripped:
                    # Clear it as the cool-off begins: the cool-off is the
                    # punishment, and a count that outlives it trips the next
                    # run before it can earn a success to clear it -- a
                    # permanent lockout rather than a pause.
                    breaker.reset()
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
                    _save_journal(journal, list_name)
                    breaker.record_failure()
                    time.sleep(random.uniform(min_gap_s, max_gap_s))
                    continue

                heading = _place_heading(page)
                if heading and not place_matches(name, heading):
                    journal[key] = "ambiguous"
                    log.warning("  Maps opened %r -- skipped", heading)
                    _save_journal(journal, list_name)
                    time.sleep(random.uniform(min_gap_s, max_gap_s))
                    continue

                # Only annotate places that are actually in the target list;
                # otherwise the note field may not even be present.
                saved_in = _saved_in(page)
                if saved_in is None or not saved_in_target(saved_in, list_name):
                    journal[key] = "not-in-list"
                    log.warning("  not in %s -- pin it first", list_name)
                    _save_journal(journal, list_name)
                    time.sleep(random.uniform(min_gap_s, max_gap_s))
                    continue

                action = note_action(_read_note(page, list_name), note)
                if action == "ok":
                    journal[key] = "ok"
                    log.info("  note already correct")
                    _save_journal(journal, list_name)
                    continue
                if action == "no-box":
                    # Counts toward the breaker: Maps changing its note editor
                    # again should stop the run after three, not walk it.
                    journal[key] = "no-note-box"
                    breaker.record_failure()
                    log.error("  no note box for %r here; nothing written",
                              list_name)
                    _save_journal(journal, list_name)
                    time.sleep(random.uniform(min_gap_s, max_gap_s))
                    continue

                ledger.record_write()
                try:
                    written = _write_note(page, note, list_name)
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
                    if breaker.is_tripped:
                        # Same as pin: checked only at the top of the loop, a
                        # run that fails its last three notes trips nothing.
                        breaker.reset()
                        ledger.start_cooloff(
                            f"{limits.max_consecutive_failures} consecutive failures"
                        )
                        raise Tripped(
                            f"Stopped after {limits.max_consecutive_failures} "
                            f"consecutive failures; a cool-off has started."
                        )
                    log.error("  note did not stick (reads %r)", written)

                _save_journal(journal, list_name)
                time.sleep(random.uniform(min_gap_s, max_gap_s))
        finally:
            try:
                ctx.close()
            except Exception as exc:
                log.debug("Browser teardown was already done: %s", exc)

    ok = sum(1 for v in journal.values() if v == "ok")
    log.info("Done. %d note(s) written or already correct.", ok)
    return journal
