"""Is the emulator ready for a sweep? Four cheap questions, asked in order.

`sweep` drives the Untappd Android app through `adb`. When any link in that
chain is missing the sweep used to find out several minutes in, as an
`adb_unavailable` or an `app_screen_unexpected` whose remedy could only guess
at the cause. These checks name the link:

    adb_on_path  ->  device_connected  ->  untappd_installed  ->  screen_size

Each later check needs the earlier ones, so a failure marks the rest as not
checked rather than running them into a confusing second error. All four are
always returned, in that order, so the shape never changes.

Read-only by design: nothing here connects, installs, launches or resizes
anything. `status` calls this, and `status` is documented as free and
side-effect-free. (`adb devices` may start the adb server daemon; that is the
one unavoidable effect, and it is the same one any adb command has.)

Never raises. A check that cannot run is a failed check with a remedy a
stranger can follow, because `status` must still answer on a machine with no
adb installed at all.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass

from .app_sweep import MAP_PACKAGE, SCREEN_WIDTH
from .config import Settings

# The sweep's gestures and map bounds are calibrated for this display. At any
# other size the pans land in the wrong place and the map reads as sparse.
SCREEN_HEIGHT = 1600
EXPECTED_SIZE = (SCREEN_WIDTH, SCREEN_HEIGHT)

# Short on purpose: `status` runs these. A healthy adb answers in well under a
# second; the first `adb devices` may spend a few starting the server.
DEVICES_TIMEOUT_S = 8.0
SHELL_TIMEOUT_S = 5.0

CHECK_NAMES = ("adb_on_path", "device_connected", "untappd_installed",
               "screen_size")

# (returncode, stdout+stderr). Injected so every branch is testable without
# an emulator.
Runner = Callable[[list[str], float], tuple[int, str]]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    remedy: str

    def to_dict(self) -> dict:
        return asdict(self)


def _default_run(argv: list[str], timeout: float) -> tuple[int, str]:
    proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    out = (proc.stdout or b"") + (proc.stderr or b"")
    return proc.returncode, out.decode("utf8", "replace")


def default_serial() -> str:
    """The serial `sweep` will drive. Deliberately not `Settings.from_env()`,
    which asks Chrome for its version -- far too slow for `status`."""
    return os.environ.get("BEERTOWN_ADB_SERIAL") or Settings().adb_serial


def _skipped(name: str, because: str) -> Check:
    return Check(name, False, f"not checked: {because}",
                 f"Fix {because.split(' ')[0]} first, then re-run "
                 f"`beertown doctor --json`.")


def _adb_on_path(which: Callable[[str], str | None]) -> Check:
    found = which("adb")
    if found:
        return Check("adb_on_path", True, found, "")
    return Check(
        "adb_on_path", False, "`adb` was not found on PATH.",
        "Install Android platform-tools "
        "(https://developer.android.com/tools/releases/platform-tools), "
        "unzip it, and add that folder to PATH. Then open a new terminal and "
        "check that `adb version` prints a version.")


def _device_connected(serial: str, run: Runner) -> Check:
    code, out = run(["adb", "devices"], DEVICES_TIMEOUT_S)
    if code != 0:
        return Check("device_connected", False,
                     f"`adb devices` exited {code}: {out.strip()[:200]}",
                     "Run `adb kill-server` and then `adb devices`, and fix "
                     "whatever it reports.")
    states = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith(("List of", "*")):
            states[parts[0]] = parts[1]
    state = states.get(serial)
    connect = (f"`adb connect {serial}`" if ":" in serial
               else "plug the device in and accept the debugging prompt")
    if state == "device":
        return Check("device_connected", True, f"{serial} is connected", "")
    if state is None:
        seen = ", ".join(f"{k} ({v})" for k, v in states.items()) or "none"
        return Check(
            "device_connected", False,
            f"{serial} is not in `adb devices` (devices seen: {seen}).",
            f"Start BlueStacks, turn on Settings -> Advanced -> Android Debug "
            f"Bridge, then run {connect}. If BlueStacks shows a different "
            f"address, set BEERTOWN_ADB_SERIAL to it.")
    return Check(
        "device_connected", False, f"{serial} is {state!r}, not 'device'.",
        f"For 'unauthorized', accept the USB debugging prompt inside the "
        f"emulator. For 'offline', run `adb disconnect {serial}` and then "
        f"{connect}.")


def _untappd_installed(serial: str, run: Runner) -> Check:
    code, out = run(["adb", "-s", serial, "shell", "pm", "list", "packages",
                     MAP_PACKAGE], SHELL_TIMEOUT_S)
    listed = {ln.strip() for ln in out.splitlines()}
    if code == 0 and f"package:{MAP_PACKAGE}" in listed:
        return Check("untappd_installed", True, MAP_PACKAGE, "")
    detail = (f"{MAP_PACKAGE} is not installed on {serial}." if code == 0
              else f"`pm list packages` exited {code}: {out.strip()[:200]}")
    return Check(
        "untappd_installed", False, detail,
        "Open the Play Store inside BlueStacks, install Untappd, open it once "
        "and sign in to your Untappd account in the app.")


def _parse_size(out: str) -> tuple[int, int] | None:
    """`wm size` prints a Physical line and, when set, an Override line.

    The override is what the app actually renders at, so it wins.
    """
    sizes: dict[str, tuple[int, int]] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        w, sep, h = value.strip().partition("x")
        if sep and w.isdigit() and h.isdigit():
            sizes[label.strip().lower()] = (int(w), int(h))
    return sizes.get("override size") or sizes.get("physical size")


def _screen_size(serial: str, run: Runner) -> Check:
    code, out = run(["adb", "-s", serial, "shell", "wm", "size"],
                    SHELL_TIMEOUT_S)
    size = _parse_size(out) if code == 0 else None
    want = f"{EXPECTED_SIZE[0]}x{EXPECTED_SIZE[1]}"
    if size == EXPECTED_SIZE:
        return Check("screen_size", True, want, "")
    detail = (f"the display is {size[0]}x{size[1]}, not {want}." if size
              else f"could not read the display size: {out.strip()[:200]}")
    return Check(
        "screen_size", False, detail,
        f"In BlueStacks go to Settings -> Display, choose portrait and a "
        f"custom resolution of {want}, then restart BlueStacks. The sweep's "
        f"gestures are calibrated for exactly that size.")


def check_emulator(serial: str | None = None, *,
                   run: Runner | None = None,
                   which: Callable[[str], str | None] | None = None,
                   ) -> list[Check]:
    """The four emulator checks, in order. Never raises."""
    serial = serial or default_serial()
    run = run or _default_run
    which = which or shutil.which
    steps: list[tuple[str, Callable[[], Check]]] = [
        ("adb_on_path", lambda: _adb_on_path(which)),
        ("device_connected", lambda: _device_connected(serial, run)),
        ("untappd_installed", lambda: _untappd_installed(serial, run)),
        ("screen_size", lambda: _screen_size(serial, run)),
    ]
    out: list[Check] = []
    failed: str | None = None
    for name, step in steps:
        if failed:
            out.append(_skipped(name, f"{failed} failed"))
            continue
        try:
            check = step()
        except subprocess.TimeoutExpired as exc:
            check = Check(name, False,
                          f"adb did not answer within {exc.timeout:g}s.",
                          "Restart BlueStacks, run `adb kill-server`, then "
                          "`adb devices`, and re-run.")
        except Exception as exc:  # noqa: BLE001 -- never raises, by contract
            check = Check(name, False, f"{type(exc).__name__}: {exc}"[:240],
                          "Check that `adb devices` runs in a terminal, then "
                          "re-run `beertown doctor --json`.")
        out.append(check)
        if not check.ok:
            failed = name
    return out


def emulator_ready(checks: list[Check]) -> bool:
    return bool(checks) and all(c.ok for c in checks)


def first_failure(checks: list[Check]) -> Check | None:
    return next((c for c in checks if not c.ok), None)
