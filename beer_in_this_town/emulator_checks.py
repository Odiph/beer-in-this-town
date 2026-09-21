"""Is the emulator ready for a sweep? Four checks, in the order they depend.

MINIMAL STAND-IN written by the UI stream (v020/b-ui) so the dashboard has
something to call. Stream A1 owns this module; the integrator keeps A1's
version and this file should simply lose the merge. The public surface is the
contract's and must not drift: `Check`, `check_emulator`, `emulator_ready`.

Never raises. A check that cannot run is a failing check with a remedy a
stranger can follow, because the dashboard shows these rows verbatim.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

UNTAPPD_PACKAGE = "com.untappdllc.app"
WANT_SIZE = (900, 1600)
DEFAULT_SERIAL = "127.0.0.1:5555"
TIMEOUT_S = 15.0

_SIZE = re.compile(r"(Physical|Override) size:\s*(\d+)x(\d+)")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    remedy: str


def _adb(*args: str) -> tuple[bool, str]:
    """Run adb; (ran_ok, output). Never raises."""
    try:
        done = subprocess.run(["adb", *args], capture_output=True, text=True,
                              timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return done.returncode == 0, (done.stdout or "") + (done.stderr or "")


def _skipped(name: str, because: str) -> Check:
    return Check(name, False, f"Not checked: {because}.",
                 "Fix the check above this one first.")


def _devices() -> tuple[bool, list[str]]:
    ok, out = _adb("devices")
    if not ok:
        return False, []
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return True, serials


def check_emulator(serial: str | None = None) -> list[Check]:
    """adb_on_path, device_connected, untappd_installed, screen_size."""
    want = serial or os.environ.get("BEERTOWN_ADB_SERIAL") or DEFAULT_SERIAL
    if shutil.which("adb") is None:
        return [
            Check("adb_on_path", False, "adb was not found on PATH.",
                  "Install Android platform-tools and add its folder to "
                  "PATH, then open a new terminal."),
            _skipped("device_connected", "adb is missing"),
            _skipped("untappd_installed", "adb is missing"),
            _skipped("screen_size", "adb is missing"),
        ]
    out = [Check("adb_on_path", True, "adb found.", "")]

    ran, serials = _devices()
    if not ran:
        return [*out,
                Check("device_connected", False, "adb devices failed.",
                      "Run `adb kill-server`, then `adb connect "
                      f"{want}` and check again."),
                _skipped("untappd_installed", "no device"),
                _skipped("screen_size", "no device")]
    if want not in serials:
        return [*out,
                Check("device_connected", False,
                      f"{want} is not connected"
                      + (f" (connected: {', '.join(serials)})" if serials else "")
                      + ".",
                      "Start BlueStacks, enable Android Debug Bridge in its "
                      f"Settings -> Advanced, then run `adb connect {want}`."),
                _skipped("untappd_installed", "no device"),
                _skipped("screen_size", "no device")]
    out.append(Check("device_connected", True, f"{want} is connected.", ""))

    ok, pkgs = _adb("-s", want, "shell", "pm", "list", "packages",
                    UNTAPPD_PACKAGE)
    installed = ok and f"package:{UNTAPPD_PACKAGE}" in pkgs
    out.append(Check(
        "untappd_installed", installed,
        "Untappd is installed." if installed else "Untappd is not installed.",
        "" if installed else "Install Untappd from the Play Store inside "
                             "BlueStacks, open it and sign in."))

    ok, size = _adb("-s", want, "shell", "wm", "size")
    found = {m.group(1): (int(m.group(2)), int(m.group(3)))
             for m in _SIZE.finditer(size)} if ok else {}
    actual = found.get("Override") or found.get("Physical")
    good = actual == WANT_SIZE
    out.append(Check(
        "screen_size", good,
        f"Screen is {actual[0]}x{actual[1]}." if actual
        else "Could not read the screen size.",
        "" if good else "In BlueStacks Settings -> Display choose portrait, "
                        "900x1600, then restart BlueStacks."))
    return out


def emulator_ready(checks: list[Check]) -> bool:
    return bool(checks) and all(c.ok for c in checks)
