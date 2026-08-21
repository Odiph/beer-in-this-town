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
import re
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


_VERSION_RE = re.compile(r"\b(\d+)\.\d+\.\d+\.\d+\b")


def chrome_major_version() -> str | None:
    """The major version of the installed Chrome, or None if it cannot be told.

    Used to keep the scraper's User-Agent honest. A UA that names a Chrome
    release which no longer exists is the cheap tell `config.DEFAULT_UA` warns
    about, and a hardcoded one goes stale silently -- it was still claiming
    Chrome 128 long after 151 was what actually ran here.

    Never raises: every failure means "use the fallback", not "stop".
    """
    exe = find_chrome()
    if exe is None:
        return None

    # Windows: Chrome keeps a versioned directory beside the binary, which is
    # cheaper and more reliable than launching it -- chrome.exe --version does
    # not print to stdout there.
    try:
        for child in sorted(exe.parent.iterdir(), reverse=True):
            if child.is_dir() and (m := _VERSION_RE.fullmatch(child.name)):
                return m.group(1)
    except OSError:
        pass

    try:
        out = subprocess.run(
            [str(exe), "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if m := _VERSION_RE.search(out.stdout or ""):
            return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


GOOGLE_SESSION_COOKIES = (
    "SID", "SSID", "HSID", "APISID", "SAPISID", "__Secure-1PSID",
)


def profile_has_google_session(profile_dir: Path) -> bool | None:
    """Is there a Google session in this profile's cookie store?

    Returns None when the answer cannot be determined — typically because
    Chrome is running and holds the database. Callers must treat None as
    "unknown", never as "no": reporting a missing login when we simply could
    not read the file would send the user round the login loop for nothing.

    Only cookie NAMES are inspected. Values are encrypted and we neither need
    nor want them.
    """
    import shutil
    import sqlite3
    import tempfile

    candidates = [
        profile_dir / "Default" / "Network" / "Cookies",
        profile_dir / "Default" / "Cookies",
    ]
    store = next((c for c in candidates if c.exists()), None)
    if store is None:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "cookies.sqlite"
        try:
            shutil.copy2(store, copy)
            con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            try:
                placeholders = ",".join("?" * len(GOOGLE_SESSION_COOKIES))
                row = con.execute(
                    "SELECT count(*) FROM cookies "
                    "WHERE host_key LIKE '%google.com' "
                    f"AND name IN ({placeholders})",
                    GOOGLE_SESSION_COOKIES,
                ).fetchone()
            finally:
                con.close()
        except (OSError, sqlite3.Error):
            return None  # locked or mid-write: unknown, not absent
    return bool(row and row[0])


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
