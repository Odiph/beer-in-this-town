"""Import the generated KML into Google My Maps.

This is the ONLY part of the pipeline that automates a Google UI, and it is
deliberately optional: the KML on disk can always be imported by hand in about
twenty seconds (mymaps.google.com -> Create a new map -> Import).

Why this beats automating the Save-to-list picker:
  - one file-input interaction instead of ~100 menu interactions
  - set_input_files does not require the element to be visible or stable, so
    the panel-reflow race that lands places in the wrong list cannot happen
  - it is a documented bulk-import feature, not a reverse-engineered flow
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

MYMAPS_NEW = "https://www.google.com/maps/d/u/0/edit"


def upload_kml(kml_path: Path, s: Settings, headless: bool = False) -> str | None:
    """Create a new My Maps map titled s.map_title and import kml_path.

    Returns the map URL, or None if the UI could not be driven (in which case
    the caller prints manual instructions -- the run is still a success).
    """
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(s.profile_dir),
            channel="chrome",
            headless=headless,
            viewport={"width": 1400, "height": 950},
            accept_downloads=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(MYMAPS_NEW, wait_until="load", timeout=60_000)

            if "accounts.google.com" in page.url:
                raise RuntimeError(
                    "Not signed in to Google in this profile. Run bootstrap first."
                )

            # 1. Title the map. Role/name locators survive class-name churn far
            #    better than CSS; each has a text fallback.
            try:
                page.get_by_text("Untitled map", exact=False).first.click(timeout=15_000)
                page.get_by_role("textbox").first.fill(s.map_title)
                page.get_by_role("button", name="Save").click()
                page.wait_for_timeout(1500)
            except PWTimeout:
                log.warning("Could not set the map title automatically; continuing.")

            # 2. Import. The file chooser is intercepted rather than clicked
            #    through, which avoids the OS dialog entirely.
            page.get_by_text("Import", exact=True).first.click(timeout=20_000)
            with page.expect_file_chooser(timeout=30_000) as fc_info:
                page.get_by_text("Select a file from your device").first.click()
            fc_info.value.set_files(str(kml_path))

            # 3. Wait for the layer to render. A KML with coordinates needs no
            #    column-mapping dialog; a CSV would.
            page.wait_for_timeout(8000)
            url = page.url
            log.info("Imported %s into My Maps: %s", kml_path.name, url)
            return url

        except Exception as exc:
            log.error("My Maps automation failed: %s", exc)
            page.screenshot(path=str(kml_path.with_suffix(".failure.png")))
            return None
        finally:
            page.wait_for_timeout(2000)
            ctx.close()


def manual_instructions(kml_path: Path, title: str) -> str:
    return (
        "\n--- Manual import (20 seconds, zero automation risk) ---\n"
        "1. Open https://www.google.com/maps/d/u/0/ and click 'Create a new map'\n"
        f"2. Click 'Untitled map' and rename it to: {title}\n"
        "3. Click 'Import' in the left panel\n"
        f"4. Choose this file: {kml_path}\n"
        "5. It appears in Google Maps under Saved -> Maps.\n"
        "   Verify the pin count matches the CSV:\n"
        "   My Maps truncates >2000 rows silently.\n"
    )
