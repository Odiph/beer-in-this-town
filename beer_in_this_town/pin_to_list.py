"""Save places into a real Google Maps *saved list* (not a My Maps layer).

Why this module exists
----------------------
A My Maps layer shows up under Saved -> Maps and has to be opened deliberately.
A saved list puts pins on the everyday map, gives full place cards, and offers
one-tap directions. There is no API for saved lists, so the only route is the
UI. This automates it as carefully as the UI allows.

Why Playwright rather than CDP synthetic clicks
-----------------------------------------------
The failure mode that ruined the manual attempt was the list picker reflowing
between "read the screen" and "click", so a click meant for one list landed on
its neighbour. Playwright avoids that specific bug:

  * its input goes through the browser real input pipeline (trusted events);
  * actionability checks re-resolve the element immediately before clicking, so
    a reflow retargets rather than mis-clicks;
  * get_by_role(name=...) matches the list by NAME, not by pixel position.

Every save is then verified by reloading the place page and reading which lists
it actually landed in. If it landed in the wrong list, the script un-saves it
and retries. Progress is journalled, so the run is resumable and idempotent.

Honest risk note
----------------
Automating the Google Maps UI is against Google Terms of Service ("do not
access the Services through automated means"), unlike the My Maps CSV/KML
import which is a supported feature. The pacing below keeps this to human-ish
speed, but the residual risk of an account warning is not zero. Use --limit for
a small trial run before committing to all 100.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import quote_plus

from .config import STATE_DIR, Settings, scope_slug
from .guardrails import (
    CircuitBreaker,
    Limits,
    RateLedger,
    Tripped,
    detect_block,
    looks_signed_out,
    single_writer,
)

log = logging.getLogger(__name__)

# Pre-scoping layout: one journal for every list on the machine, so a
# place saved into one list counted as done for all of them.
LEGACY_JOURNAL = STATE_DIR / "pinned.json"


def journal_path(list_name: str) -> Path:
    """A journal records what was written to *one* saved list."""
    return STATE_DIR / f"pinned_{scope_slug(list_name)}.json"

# Pacing between places. Saving is a write, so this is deliberately slower than
# the read-only scraper's 2.0-4.5s.
#
# These are THE defaults: the CLI derives --min-gap/--max-gap from them rather
# than repeating literals, because it previously did repeat them and drifted.
# The module said 4-9s while every real run went at 8-16s, so anyone reading
# this file to find out how hard the tool hits Google got an answer half the
# true value. One number to change, one number to trust.
MIN_GAP_S = 8.0
MAX_GAP_S = 16.0
MAX_ATTEMPTS = 3
# Consecutive lookup misses that mean 'blocked', not 'bad data'.
MAX_CONSECUTIVE_MISSES = 8

SAVE_BTN = "button[aria-label^='Save'], button[aria-label^='Saved']"


def journal_key(name: str, address: str | None) -> str:
    """Identify a place by name AND address.

    Keying on the bare name silently collapsed chains: two outlets both called
    "Harry's" or "Brewerkz" shared one entry, so the second was skipped as
    already done and never pinned -- invisible in the summary counts.
    """
    return f"{name} | {address}" if address else name


def _load_journal(list_name: str) -> dict[str, str]:
    path = journal_path(list_name)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if LEGACY_JOURNAL.exists():
        # The pre-scoping journal does not record which list it was built for,
        # so adopting it is a guess. Make it exactly once, by renaming: a
        # second list inheriting "already saved" entries it never earned would
        # skip real work and under-deliver in silence.
        log.warning(
            "Adopting the pre-scoping pinned.json as the journal for %r, on the "
            "assumption it was built for that list. Any other list starts "
            "empty. Rename it back if that assumption is wrong.", list_name,
        )
        LEGACY_JOURNAL.replace(path)
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _lookup(journal: dict[str, str], name: str, address: str | None) -> str | None:
    """Read an entry, honouring journals written before keys included address."""
    return journal.get(journal_key(name, address)) or journal.get(name)


def _save_journal(journal: dict[str, str], list_name: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    journal_path(list_name).write_text(
        json.dumps(journal, indent=1, ensure_ascii=False), encoding="utf-8"
    )


def _search_url(name: str, address: str | None, region: str | None = None) -> str:
    """Build a Maps search URL.

    The region qualifier matters: "Brewerkz 1 Fullerton Road" without a country
    can match the wrong continent entirely. It is appended only when the address
    does not already name it.
    """
    query = f"{name} {address}" if address else name
    if region and region.lower() not in query.lower():
        query = f"{query}, {region}"
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query)


class AmbiguousList(RuntimeError):
    """The requested list name does not identify exactly one list.

    Raised rather than resolved. Picking the nearest label is how a place
    lands in somebody else's list, and the verification step cannot catch it
    afterwards because it is looking at the list that was actually clicked.
    """


# A picker row's accessible name often carries a place count -- "Bars (12)",
# "Bars 12 places". That is the same list, so the count is stripped before
# names are compared; nothing else about the label is.
_COUNT_SUFFIX = re.compile(r"\s*(\(\d+\)|·?\s*\d+\s+places?)\s*$", re.I)


def _list_key(name: str) -> str:
    """Compare list names case- and padding-insensitively, and nothing more."""
    return _COUNT_SUFFIX.sub("", (name or "").strip()).strip().casefold()


def saved_in_names(text: str) -> set[str]:
    """The list names out of a "Saved in ..." line, which may name several."""
    return {part.strip() for part in (text or "").split(",") if part.strip()}


def saved_in_target(text: str, list_name: str) -> bool:
    """Is the place in EXACTLY the list we asked for?

    Substring matching here reported success for the wrong list: an account
    with "Bars" and "London Bars" saves into the latter, the reload reads
    "Saved in London Bars", and `"bars" in "london bars"` is True. The run
    then journals `ok` and never retries it.
    """
    want = _list_key(list_name)
    return any(_list_key(n) == want for n in saved_in_names(text))


def pick_list_row(row_names: list[str], list_name: str) -> int:
    """Index of the row that IS the requested list. Never the nearest one."""
    want = _list_key(list_name)
    hits = [i for i, n in enumerate(row_names) if _list_key(n) == want]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise AmbiguousList(
            f"No list in the picker is named exactly {list_name!r}. It offered: "
            f"{', '.join(repr(n) for n in row_names) or '(nothing)'}. Nothing "
            f"was saved -- clicking the closest name is how a place lands in "
            f"the wrong list."
        )
    raise AmbiguousList(
        f"{len(hits)} lists are named {list_name!r}. Rename one in Google Maps "
        f"so the target is unambiguous; guessing between them is not safe."
    )


def list_exists_in(body: str, list_name: str) -> bool:
    """Does the saved-lists page carry this list, as a line of its own?

    Checking `list_name.lower() in body.lower()` passed for "Bars" whenever
    "London Bars" existed, so a run could clear pre-flight against a list the
    user does not have.
    """
    want = _list_key(list_name)
    return any(_list_key(line) == want for line in (body or "").splitlines())


def _saved_in(page) -> str | None:
    """Read the "Saved in <list>" line from the open place panel.

    Returns "" when the place is definitively in no list, and **None when we
    could not tell**. The distinction is not pedantic: collapsing "unknown" to
    "not saved" means a transient read failure on an already-saved place makes
    us click its (checked) row, which UNCHECKS it. A run could then leave the
    user's list smaller than it found it.
    """
    try:
        node = page.get_by_text("Saved in ", exact=False).first
        if node.count() == 0:
            return ""
        return node.inner_text(timeout=3000).replace("Saved in ", "").strip()
    except Exception as exc:
        log.debug("Could not read the saved-in line: %s", exc)
        return None


def _open_place(page, name: str, address: str | None, region: str | None) -> bool:
    """Search for a place and wait for its detail panel. False if not found."""
    page.goto(
        _search_url(name, address, region),
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    try:
        # Either a place panel (single hit) or a results list (ambiguous query).
        page.wait_for_selector(f"{SAVE_BTN}, div[role='feed']", timeout=25_000)
    except Exception:
        return False

    # Ambiguous query -> take the first result, which is what a human would do.
    feed = page.locator("div[role='feed'] a[href*='/maps/place/']")
    if feed.count() > 0:
        feed.first.click()
        try:
            page.wait_for_selector(SAVE_BTN, timeout=20_000)
        except Exception:
            return False
    return True


def _save_button(page):
    return page.locator(SAVE_BTN).first


def _normalise(text: str) -> set[str]:
    """Word set for loose name comparison: lowercase, alphanumeric only."""
    return {
        "".join(ch for ch in word.lower() if ch.isalnum())
        for word in text.split()
    } - {""}


def place_matches(requested: str, heading: str, threshold: float = 0.5) -> bool:
    """Does the open place plausibly correspond to the one we asked for?

    Google rewrites names ("Brewerkz" -> "Brewerkz Riverside Point", "TAP -
    9 Penang" -> "TAP Craft Beer Bar"), so exact matching is useless. What we
    are guarding against is the genuinely different venue: searching a generic
    name and silently saving somebody else's bar, then journalling it "ok".

    Overlap is measured against the REQUESTED name, so extra words Google adds
    cost nothing while missing words are penalised.
    """
    want, got = _normalise(requested), _normalise(heading)
    if not want:
        return True
    return len(want & got) / len(want) >= threshold


def _place_heading(page) -> str | None:
    try:
        node = page.locator("h1").first
        if node.count() == 0:
            return None
        return node.inner_text(timeout=3000).strip()
    except Exception:
        return None


def _assert_ready(page, list_name: str) -> None:
    """Fail fast unless we are signed in AND the target list already exists.

    Checking the URL for accounts.google.com is NOT enough: signed-out Google
    Maps loads perfectly happily, and we would then fail on all 100 places
    one at a time instead of stopping immediately.
    """
    page.goto(
        "https://www.google.com/maps", wait_until="domcontentloaded", timeout=60_000
    )
    page.wait_for_timeout(5000)

    # Probe the DOM, not the body text: a signed-out Maps page still renders
    # the word "Saved" in places, so text matching gives a false positive and
    # you end up blaming a missing list when the real problem is no session.
    signed_out = page.locator(
        "a[href*='ServiceLogin'], a[aria-label*='Sign in'], a:has-text('Sign in')"
    ).count()
    account = page.locator(
        "a[aria-label*='Google Account'], img[alt*='Google Account']"
    ).count()
    if account == 0 and signed_out > 0:
        raise RuntimeError(
            "Not signed in to Google in this Chrome profile.\n"
            "This is a dedicated profile, separate from your everyday Chrome,\n"
            "so it needs its own one-time login:\n"
            "  python -m beer_in_this_town bootstrap"
        )

    page.goto(
        "https://www.google.com/maps/@/data=!4m2!10m1!1e1",
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    page.wait_for_timeout(4000)
    body = page.locator("body").inner_text(timeout=15_000)

    if not list_exists_in(body, list_name):
        raise RuntimeError(
            f"Saved list {list_name!r} not found in this account.\n"
            "This tool saves INTO an existing list; it does not create one.\n"
            "Create it by hand in Google Maps (Saved -> New list), then re-run.\n"
            "If you are sure it exists, check the name matches exactly."
        )
    log.info("Pre-flight OK: signed in, list %r exists.", list_name)


def _pin_once(page, list_name: str) -> str | None:
    """Open the picker, click the target list by name, return the verified list.

    Returns the "Saved in ..." text after a reload, so the caller can tell the
    difference between success, wrong-list, and no-op.
    """
    place_url = page.url

    _save_button(page).click(timeout=15_000)

    # Let the picker finish opening before touching it. Without this the first
    # two attempts are reliably swallowed: the menu is still animating, Maps
    # discards the click, and we burn three interactions per place instead of
    # one -- slower, and three times the footprint for a rate-limited script.
    candidates = (
        page.get_by_role("menuitemradio", name=list_name, exact=False)
        .or_(page.get_by_role("menuitemcheckbox", name=list_name, exact=False))
    )
    candidates.first.wait_for(state="visible", timeout=15_000)
    page.wait_for_timeout(1200)  # settle: the menu animates after it is visible

    # Match the row by its accessible name. Position is irrelevant, so a reflow
    # cannot make this hit the neighbouring list -- the bug that put a Singapore
    # bar into a London list during the manual attempt.
    #
    # The role query is a substring match, so it offers "London Bars" for a
    # request of "Bars" too. Resolving that with .first is how the wrong list
    # gets clicked, and the verification below cannot see it afterwards.
    names = candidates.all_inner_texts()
    row = candidates.nth(pick_list_row(names, list_name))
    row.click(timeout=15_000)

    # Dismiss the picker and give Maps time to actually commit the write before
    # verifying. Reloading too early reads the OLD state, which reads as a
    # failure -- and the retry then toggles the place straight back off again.
    page.keyboard.press("Escape")
    page.wait_for_timeout(4000)

    # The picker does not reliably re-render, so never trust it. Reload and read
    # the place panel instead.
    page.goto(place_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector(SAVE_BTN, timeout=25_000)
    page.wait_for_timeout(1500)
    return _saved_in(page)


def _unsave(page, wrong_list: str) -> None:
    """Undo a save that landed in the wrong list."""
    try:
        _save_button(page).click(timeout=10_000)
        # Exact, like the other three sites. Undoing against a substring match
        # can uncheck a different list than the one that was wrongly saved to,
        # which turns one bad save into two.
        rows = page.get_by_role("menuitemradio", name=wrong_list, exact=False)
        row = rows.nth(pick_list_row(rows.all_inner_texts(), wrong_list))
        row.click(timeout=10_000)
        page.wait_for_timeout(2000)
        page.keyboard.press("Escape")
        log.warning("Un-saved from wrong list %r", wrong_list)
    except Exception as exc:
        log.error(
            "Could not undo wrong-list save (%s): remove %r by hand", exc, wrong_list
        )


def _abort_if_blocked(page, ledger: RateLedger) -> None:
    """Stop the run the moment Google shows an interstitial or signs us out.

    These pages are served as HTTP 200, so there is no error to catch -- the
    only signal is the text on screen. Continuing past one is the single
    fastest way to turn a warning into a ban.
    """
    try:
        text = page.locator("body").inner_text(timeout=10_000)
    except Exception:
        return  # a transient read failure is not evidence of a block

    signal = detect_block(text)
    if signal:
        ledger.start_cooloff(f"block signal on page: {signal!r}")
        raise Tripped(
            f"Google served an interstitial ({signal!r}). Stopping immediately "
            "and starting a cool-off. Open Google Maps in a normal browser, "
            "confirm the account is healthy, and try again later."
        )

    if looks_signed_out(text):
        ledger.start_cooloff("signed out mid-run")
        raise Tripped(
            "Signed out mid-run -- the session was invalidated. Stopping. "
            "Re-run bootstrap and check the account before continuing."
        )


@single_writer
def pin_places(
    places: list[tuple[str, str | None]],
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
    """Save each (name, address) into the named Google Maps list.

    Resumable: anything already marked done in state/pinned.json is skipped, so
    re-running after an interruption costs nothing and cannot double-save.
    """
    from playwright.sync_api import sync_playwright

    limits = limits or Limits()
    ledger = RateLedger(limits)
    breaker = CircuitBreaker(limits.max_consecutive_failures)

    # Fail closed BEFORE opening a browser: cool-off and daily budget first.
    ledger.assert_can_start()

    journal = _load_journal(list_name)
    todo = [(n, a) for (n, a) in places if _lookup(journal, n, a) != "ok"]

    allowed = ledger.budget_for_this_run(limit)
    if len(todo) > allowed:
        log.warning(
            "Trimming this run to %d place(s): per-run cap %d, %d left in the "
            "rolling 24h budget. Re-run later to continue -- progress resumes.",
            allowed, limits.max_per_run, ledger.remaining_today(),
        )
        todo = todo[:allowed]

    log.info(
        "%d place(s) to pin into %r (%d already done, %d writes left today)",
        len(todo), list_name, sum(1 for v in journal.values() if v == "ok"),
        ledger.remaining_today(),
    )
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
        consecutive_misses = 0
        try:
            _assert_ready(page, list_name)

            for i, (name, address) in enumerate(todo, 1):
                log.info("[%3d/%d] %s", i, len(todo), name)

                # --- guardrails, checked before every single write ---------
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
                        f"Stopped after {breaker.consecutive} consecutive failures. "
                        "Repeated failures are exactly when a script looks least "
                        "human, so this stops rather than retries. Cool-off set; "
                        "progress is journalled and resumes."
                    )

                if ledger.remaining_today() <= 0:
                    log.warning("Daily write budget reached -- stopping cleanly.")
                    break

                # A longer, human-shaped pause every so often.
                if i > 1 and (i - 1) % limits.break_every == 0:
                    log.info("  taking a %.0fs break", limits.break_seconds)
                    time.sleep(limits.break_seconds)

                if not _open_place(page, name, address, region):
                    journal[journal_key(name, address)] = "not-found"
                    log.warning("  no Google Maps result -- skipped")
                    _save_journal(journal, list_name)
                    # A miss still counts toward the breaker and still waits.
                    # Without this, a block page whose wording we do not
                    # recognise makes every lookup "fail" and the run
                    # machine-guns page loads with no gap and no trip -- the
                    # least human-looking thing it could possibly do.
                    consecutive_misses += 1
                    if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                        ledger.start_cooloff(
                            f"{consecutive_misses} consecutive lookup misses"
                        )
                        raise Tripped(
                            f"{consecutive_misses} places in a row could not be "
                            "found. That is not a data problem -- the session is "
                            "blocked or broken. Stopping; cool-off set."
                        )
                    time.sleep(random.uniform(min_gap_s, max_gap_s))
                    continue
                consecutive_misses = 0

                heading = _place_heading(page)
                if heading and not place_matches(name, heading):
                    # Saving the wrong venue and journalling it "ok" is the same
                    # silent-wrong-outcome family as the wrong-list bug, one
                    # level up. Refuse rather than guess.
                    journal[journal_key(name, address)] = "ambiguous"
                    log.warning("  Maps opened %r, which does not match -- "
                                "skipped", heading)
                    _save_journal(journal, list_name)
                    time.sleep(random.uniform(min_gap_s, max_gap_s))
                    continue

                already = _saved_in(page)
                if already is None:
                    # Unknown state. Clicking now could uncheck an already-saved
                    # place, so leave it alone and retry on a later run.
                    log.warning("  could not read the saved-in line -- skipping "
                                "this place rather than risk un-saving it")
                    breaker.record_failure()
                    time.sleep(random.uniform(min_gap_s, max_gap_s))
                    continue
                if saved_in_target(already, list_name):
                    journal[journal_key(name, address)] = "ok"
                    log.info("  already in %s", list_name)
                    _save_journal(journal, list_name)
                    continue

                outcome = "failed"
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    # Charge the budget BEFORE the interaction. Clicking Save is
                    # a real mutation request whether or not our verification
                    # later agrees, and each retry is another one. Counting only
                    # verified successes let a flaky day put ~3x the believed
                    # traffic through the account.
                    ledger.record_write()
                    try:
                        landed = _pin_once(page, list_name)
                    except AmbiguousList:
                        # Never per-place and never worth retrying: the list
                        # name does not change between places, so every
                        # remaining one would fail the same way -- three
                        # budget charges and a breaker failure each, until the
                        # breaker trips and starts a six-hour cool-off. Caught
                        # here it was invisible: cmd_pin's handler could not
                        # fire and `list_ambiguous` was unreachable.
                        raise
                    except Exception as exc:
                        log.warning("  attempt %d error: %s", attempt, exc)
                        # An interstitial can appear mid-attempt; without this
                        # check the retries hammer a blocked page.
                        _abort_if_blocked(page, ledger)
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(1500)
                        continue

                    if landed is None:
                        log.warning("  attempt %d: could not verify -- not "
                                    "assuming either way", attempt)
                        page.wait_for_timeout(2000)
                        continue
                    if saved_in_target(landed, list_name):
                        outcome = "ok"
                        log.info("  saved (verified: %r)", landed)
                        break
                    if landed and landed != already:
                        # Landed somewhere unintended -- undo before retrying so
                        # we never leave debris in someone else's list.
                        _unsave(page, landed)
                    log.warning(
                        "  attempt %d did not stick (panel says %r)",
                        attempt, landed or "not saved",
                    )
                    page.wait_for_timeout(2000)

                journal[journal_key(name, address)] = outcome
                _save_journal(journal, list_name)

                if outcome == "ok":
                    breaker.record_success()
                else:
                    breaker.record_failure()
                    log.error("  giving up on %s after %d attempts", name, MAX_ATTEMPTS)
                    # Check here, not only at the top of the next iteration.
                    # Checked only there, a run that fails its last three
                    # places trips nothing and sets no cool-off -- and the UI
                    # having stopped responding is exactly when a script looks
                    # least human, so the end of a run is the worst moment to
                    # stop watching.
                    if breaker.is_tripped:
                        breaker.reset()
                        ledger.start_cooloff(
                            f"{limits.max_consecutive_failures} consecutive failures"
                        )
                        raise Tripped(
                            f"Stopped after {limits.max_consecutive_failures} "
                            f"consecutive failures. The UI is not behaving as "
                            f"expected; a cool-off has started."
                        )

                time.sleep(random.uniform(min_gap_s, max_gap_s))
        finally:
            # The browser may already be gone (crash, or the user closed it).
            # Teardown must never mask a completed run: the journal is safe on
            # disk either way.
            try:
                ctx.close()
            except Exception as exc:
                log.debug("Browser teardown was already done: %s", exc)

    ok = sum(1 for v in journal.values() if v == "ok")
    log.info(
        "Done. %d saved, %d failed, %d not found.",
        ok,
        sum(1 for v in journal.values() if v == "failed"),
        sum(1 for v in journal.values() if v == "not-found"),
    )
    return journal


def region_from_csv(path: Path) -> str | None:
    """The city these venues are in, read from the data rather than assumed.

    `--region` is appended to every Maps lookup so a bare name cannot match a
    venue in another country. Defaulting it to one city did the opposite: a
    London CSV searched "..., London, Singapore", and what came back could be
    saved against the wrong place entirely.

    Returns None when the CSV carries no usable city, so the caller can say
    the guard is off rather than quietly appending nothing.
    """
    import csv
    from collections import Counter

    with path.open(encoding="utf-8-sig", newline="") as fh:
        cities = Counter(
            (r.get("city") or "").strip() for r in csv.DictReader(fh)
        )
    cities.pop("", None)
    if not cities:
        return None
    return cities.most_common(1)[0][0]


def places_from_csv(path: Path) -> list[tuple[str, str | None]]:
    """Read (name, address) pairs from any CSV this project writes."""
    import csv

    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    out: list[tuple[str, str | None]] = []
    for r in rows:
        name = (r.get("name") or r.get("Name") or "").strip()
        if not name:
            continue
        address = (r.get("address") or r.get("Address") or "").strip() or None
        city = (r.get("city") or "").strip()
        if address and city and city.lower() not in address.lower():
            address = f"{address}, {city}"
        elif not address and city:
            address = city
        out.append((name, address))
    return out
