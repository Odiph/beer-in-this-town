"""Drive a real Android device (BlueStacks, here) through `adb`.

The transport half of `app_sweep.Device`. Everything interesting lives in
`app_map.py` and `app_sweep.py`; this is the part that actually touches the
emulator, kept as thin and as loud as possible.

Three details are not obvious and each cost time against the live app:

- `uiautomator dump /dev/tty` writes the XML to the terminal with a status
  line in front of it, so the payload has to be carved out by locating the
  declaration rather than parsing stdout whole.
- Every call carries `-s <serial>`. This project's BlueStacks config gives
  two instances the same adb port, and an unaddressed command reaches
  whichever device it likes.
- **A failed call raises.** Returning empty output would be read one layer up
  as an empty screen, and an empty screen reads as a place with no bars --
  which is the failure shape this whole subsystem is built to avoid.

No guardrail lives here. The sweep only reads, but it reads through a real
signed-in account, so it stays human-invoked and out of `next_actions` for
the same reason `pin` and `notes` are.
"""
from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# `mCurrentFocus=Window{ae16c7d u0 com.untappdllc.app/com.untappdllc...}`
_FOCUS = re.compile(r"mCurrentFocus=\S+\s+\S+\s+(?P<pkg>[^/\s}]+)")

DEFAULT_TIMEOUT_S = 120.0

# `    gps: Location[gps 32.075318,34.808611 acc=1 ...]`
_FIX = re.compile(
    r"^\s*(?P<provider>gps|network):\s*Location\[\S+\s+"
    r"(?P<lat>-?\d+\.\d+),(?P<lng>-?\d+\.\d+)", re.MULTILINE)

# What a transiently killed `uiautomator` prints instead of a dump. Seen live:
# it ended a depth-3 Singapore sweep partway through, with nothing wrong on
# the device -- the usual cause is two dump instances colliding.
_KILLED = "Killed"


class AdbUnavailable(RuntimeError):
    """An `adb` call failed, or returned something unusable.

    Deliberately not a silent empty result. "The device did not answer" and
    "there is nothing here" want opposite responses and are impossible to
    tell apart from an empty list.
    """


def _default_run(argv: list[str], timeout: float) -> str:
    proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise AdbUnavailable(
            f"{' '.join(argv)} exited {proc.returncode}: "
            f"{proc.stderr.decode('utf8', 'replace')[:200]}")
    return proc.stdout.decode("utf8", "replace")


@dataclass
class AdbDevice:
    """An `adb`-attached device, addressed by serial.

    `run` is injected so the whole driver is testable without hardware.
    """

    serial: str
    run: Callable[[list[str], float], str] = field(default=_default_run)
    timeout_s: float = DEFAULT_TIMEOUT_S

    def _adb(self, *args: str) -> str:
        argv = ["adb", "-s", self.serial, *args]
        try:
            return self.run(argv, self.timeout_s)
        except AdbUnavailable:
            raise
        except Exception as exc:  # transport, timeout, missing binary
            raise AdbUnavailable(f"{' '.join(argv)} failed: {exc}") from exc

    # --- app_sweep.Device ------------------------------------------------

    def focused_package(self) -> str:
        """Which app is in front. Checked before anything is harvested.

        A harvest once captured the BlueStacks launcher and reported its five
        nodes as data; this is what makes that loud instead.
        """
        out = self._adb("shell", "dumpsys", "window")
        for line in out.splitlines():
            if "mCurrentFocus" in line:
                m = _FOCUS.search(line)
                if m:
                    return m.group("pkg")
                # Report what was actually seen rather than inventing a
                # package name that would quietly pass the guard.
                return line.split("=", 1)[-1].strip().rstrip("}")
        raise AdbUnavailable("dumpsys window reported no mCurrentFocus")

    def dump(self) -> str:
        """The current screen's accessibility tree, as XML.

        Retries **once**, and only on a killed `uiautomator`, which is
        transient. A second kill is not transient, and anything else that is
        not XML is news rather than noise, so both raise.
        """
        raw = self._adb("exec-out", "uiautomator", "dump", "/dev/tty")
        if "<?xml" not in raw and raw.strip() == _KILLED:
            log.warning("uiautomator was killed mid-dump; retrying once.")
            raw = self._adb("exec-out", "uiautomator", "dump", "/dev/tty")
        start = raw.find("<?xml")
        end = raw.rfind(">")
        if start < 0 or end <= start:
            raise AdbUnavailable(
                f"uiautomator returned no XML: {raw.strip()[:200]!r}")
        return raw[start:end + 1]

    def location(self) -> tuple[float, float]:
        """The device's own position fix, as `(lat, lng)`.

        This is what calibrates the sweep's camera: after `Reset location`
        the map centres on it to within 24 m, with no corpus, geocoder or
        network. GPS is preferred over the network provider, which is
        coarser.

        Raises rather than returning a default. A fallback location would
        calibrate an entire corpus to somewhere real and wrong.
        """
        out = self._adb("shell", "dumpsys", "location")
        fixes = {m.group("provider"): (float(m.group("lat")),
                                       float(m.group("lng")))
                 for m in _FIX.finditer(out)}
        for provider in ("gps", "network"):
            if provider in fixes:
                return fixes[provider]
        raise AdbUnavailable(
            "dumpsys location reported no location fix. Set one in the "
            "emulator's location settings; the sweep will not guess one.")

    def tap(self, x: int, y: int) -> None:
        self._adb("shell", "input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int) -> None:
        self._adb("shell", "input", "swipe",
                  str(x1), str(y1), str(x2), str(y2), str(ms))

    def type_text(self, text: str) -> None:
        """`input text` takes %s for a space and nothing for a newline."""
        self._adb("shell", "input", "text", text.replace(" ", "%s"))

    def press_enter(self) -> None:
        self._adb("shell", "input", "keyevent", "KEYCODE_ENTER")

    def launch(self, package: str) -> None:
        """Start the app fresh, from its home screen.

        **Stopped first**, because `monkey` on a running app only resumes it
        on whatever screen it was on. Found live: it brought back a category
        filter panel a crashed sweep had left open, and the recovery that
        followed tapped into it believing it was on Discover.

        `monkey` is used rather than `am start` because it needs no activity
        name, and the activity has changed between app versions.
        """
        self._adb("shell", "am", "force-stop", package)
        self._adb("shell", "monkey", "-p", package,
                  "-c", "android.intent.category.LAUNCHER", "1")
