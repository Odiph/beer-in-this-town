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
import time
from pathlib import Path
from urllib.parse import quote_plus

from .config import STATE_DIR, Settings

log = logging.getLogger(__name__)

JOURNAL = STATE_DIR / "pinned.json"

# Pacing. Saving a place is a write; go slower than the read-only scraper.
MIN_GAP_S = 4.0
MAX_GAP_S = 9.0
MAX_ATTEMPTS = 3

SAVE_BTN = "button[aria-label^='Save'], button[aria-label^='Saved']"


def _load_journal() -> dict[str, str]:
    if JOURNAL.exists():
        return json.loads(JOURNAL.read_text(encoding="utf-8"))
    return {}


def _save_journal(journal: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(
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


def _saved_in(page) -> str:
    """Read the "Saved in <list>" line from the open place panel.

    Returns an empty string when the place is in no list. This is the ground
    truth we verify against -- the picker checkmarks refresh unreliably.
    """
    try:
        node = page.get_by_text("Saved in ", exact=False).first
        if node.count() == 0:
            return ""
        return node.inner_text(timeout=3000).replace("Saved in ", "").strip()
    except Exception:
        return ""


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
            "  python -m untappd_maps bootstrap"
        )

    page.goto(
        "https://www.google.com/maps/@/data=!4m2!10m1!1e1",
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    page.wait_for_timeout(4000)
    body = page.locator("body").inner_text(timeout=15_000)

    if list_name.lower() not in body.lower():
        raise RuntimeError(
            f"Saved list {list_name!r} not found in this account.\n"
            "This tool saves INTO an existing list; it does not create one.\n"
            "Create it by hand in Google Maps (Saved -> New list), then re-run.\n"
            "If you are sure it exists, check the name matches exactly."
        )
    log.info("Pre-flight OK: signed in, list %r exists.", list_name)


def _pin_once(page, list_name: str) -> str:
    """Open the picker, click the target list by name, return the verified list.

    Returns the "Saved in ..." text after a reload, so the caller can tell the
    difference between success, wrong-list, and no-op.
    """
    place_url = page.url

    _save_button(page).click(timeout=15_000)

    # Match the row by its accessible name. Position is irrelevant, so a reflow
    # cannot make this hit the neighbouring list -- the bug that put a Singapore
    # bar into a London list during the manual attempt.
    row = (
        page.get_by_role("menuitemradio", name=list_name, exact=False)
        .or_(page.get_by_role("menuitemcheckbox", name=list_name, exact=False))
        .first
    )
    row.click(timeout=15_000)

    # The picker does not reliably re-render, so never trust it. Reload and read
    # the place panel instead.
    page.wait_for_timeout(2500)
    page.goto(place_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector(SAVE_BTN, timeout=25_000)
    return _saved_in(page)


def _unsave(page, wrong_list: str) -> None:
    """Undo a save that landed in the wrong list."""
    try:
        _save_button(page).click(timeout=10_000)
        row = page.get_by_role("menuitemradio", name=wrong_list, exact=False).first
        row.click(timeout=10_000)
        page.wait_for_timeout(2000)
        page.keyboard.press("Escape")
        log.warning("Un-saved from wrong list %r", wrong_list)
    except Exception as exc:
        log.error(
            "Could not undo wrong-list save (%s): remove %r by hand", exc, wrong_list
        )


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
) -> dict[str, str]:
    """Save each (name, address) into the named Google Maps list.

    Resumable: anything already marked done in state/pinned.json is skipped, so
    re-running after an interruption costs nothing and cannot double-save.
    """
    from playwright.sync_api import sync_playwright

    journal = _load_journal()
    todo = [(n, a) for (n, a) in places if journal.get(n) != "ok"]
    if limit:
        todo = todo[:limit]

    log.info(
        "%d place(s) to pin into %r (%d already done)",
        len(todo), list_name, sum(1 for v in journal.values() if v == "ok"),
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
        try:
            _assert_ready(page, list_name)

            for i, (name, address) in enumerate(todo, 1):
                log.info("[%3d/%d] %s", i, len(todo), name)

                if not _open_place(page, name, address, region):
                    journal[name] = "not-found"
                    log.warning("  no Google Maps result -- skipped")
                    _save_journal(journal)
                    continue

                already = _saved_in(page)
                if list_name.lower() in already.lower():
                    journal[name] = "ok"
                    log.info("  already in %s", list_name)
                    _save_journal(journal)
                    continue

                outcome = "failed"
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        landed = _pin_once(page, list_name)
                    except Exception as exc:
                        log.warning("  attempt %d error: %s", attempt, exc)
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(1500)
                        continue

                    if list_name.lower() in landed.lower():
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

                journal[name] = outcome
                _save_journal(journal)
                if outcome == "failed":
                    log.error("  giving up on %s after %d attempts", name, MAX_ATTEMPTS)

                time.sleep(random.uniform(min_gap_s, max_gap_s))
        finally:
            ctx.close()

    ok = sum(1 for v in journal.values() if v == "ok")
    log.info(
        "Done. %d saved, %d failed, %d not found.",
        ok,
        sum(1 for v in journal.values() if v == "failed"),
        sum(1 for v in journal.values() if v == "not-found"),
    )
    return journal


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
