"""Launching a *normal* Chrome, on purpose.

Google refuses to complete a sign-in inside an automation-controlled browser
("Couldn't sign you in -- This browser or app may not be secure"). Playwright
sets automation flags on the browsers it launches, so the login flow is a dead
end there no matter how the profile is configured.

The workaround is a split:

  1. **Log in** using a plain Chrome process we start ourselves, with no
     automation flags and its own `--user-data-dir`. Google is happy: as far as
     it can tell this is somebody using Chrome.
  2. **Reuse** that profile from Playwright afterwards. Google blocks the
     sign-in *flow*, not an existing session, so the saved cookies keep working.

This module owns step 1. It deliberately does not use Playwright at all.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

WINDOWS_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

POSIX_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


def find_chrome() -> Path | None:
    """Locate a real Chrome binary, or None."""
    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium"):
        found = shutil.which(name)
        if found:
            return Path(found)

    local = os.environ.get("LOCALAPPDATA")
    candidates: list[str] = list(WINDOWS_CANDIDATES) + list(POSIX_CANDIDATES)
    if local:
        candidates.insert(
            0, str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe")
        )

    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def launch_for_login(
    profile_dir: Path, url: str = "https://accounts.google.com/"
) -> subprocess.Popen | None:
    """Start a clean Chrome on `profile_dir` for the human to log in with.

    No automation flags, no CDP port, no Playwright. The only unusual argument
    is the profile directory, which is what lets us pick the session up later.
    """
    chrome = find_chrome()
    if chrome is None:
        return None

    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        str(chrome),
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    log.info("Launching Chrome for login: %s", chrome)
    return subprocess.Popen(args)
