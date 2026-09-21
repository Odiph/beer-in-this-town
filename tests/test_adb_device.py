"""Driving a real device through `adb`, with the transport injected.

Small surface, but three things here were learned the hard way against
BlueStacks and are worth holding with tests:

- `uiautomator dump /dev/tty` prints the XML with shell noise around it, so
  the payload has to be carved out rather than parsed whole.
- `dumpsys window` reports the focused window as a long string; the package
  is a fragment of it, not the whole thing.
- A failed `adb` call must raise. Returning empty output would read as "an
  empty screen", and an empty screen reads as "a place with no bars".

No device, no network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.adb_device import AdbDevice, AdbUnavailable

FOCUS = ("  mCurrentFocus=Window{ae16c7d u0 "
         "com.untappdllc.app/com.untappdllc.app.MainActivityDefault}")

DUMP = ("UI hierchary dumped to: /dev/tty<?xml version='1.0'?>"
        "<hierarchy><node class='android.view.View' content-desc='Lauter.'"
        " bounds='[1,2][3,4]'/></hierarchy>")


class FakeRunner:
    """Stands in for `subprocess.run`, recording the argv it was given."""

    def __init__(self, replies):
        self.replies = replies
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        for key, value in self.replies.items():
            if key in " ".join(argv):
                if isinstance(value, Exception):
                    raise value
                return value
        return ""


def _device(replies):
    runner = FakeRunner(replies)
    return AdbDevice(serial="127.0.0.1:5555", run=runner), runner


def test_the_serial_is_passed_to_every_call():
    """Two BlueStacks instances share port 5555 in this project's config, and
    more than one emulator can be attached. An unaddressed `adb` command
    reaches whichever device it likes."""
    dev, runner = _device({"dumpsys": FOCUS})
    dev.focused_package()
    assert runner.calls[0][:3] == ["adb", "-s", "127.0.0.1:5555"]


def test_the_focused_package_is_carved_out_of_dumpsys():
    dev, _ = _device({"dumpsys": FOCUS})
    assert dev.focused_package() == "com.untappdllc.app"


def test_an_unparseable_focus_line_is_reported_not_guessed():
    dev, _ = _device({"dumpsys": "mCurrentFocus=null"})
    assert dev.focused_package() == "null"


def test_the_xml_is_carved_out_of_the_dump_noise():
    """`uiautomator dump /dev/tty` prefixes its output with a status line and
    can trail whitespace. Parsing the raw stdout fails."""
    dev, _ = _device({"uiautomator": DUMP})
    xml = dev.dump()
    assert xml.startswith("<?xml")
    assert xml.endswith(">")
    assert "UI hierchary" not in xml


def test_a_dump_with_no_xml_raises_rather_than_returning_nothing():
    """An empty return would be read as an empty screen, and an empty screen
    reads as a place with no venues."""
    dev, _ = _device({"uiautomator": "ERROR: could not get idle state"})
    with pytest.raises(AdbUnavailable, match="no XML"):
        dev.dump()


def test_a_transport_failure_raises():
    dev, _ = _device({"uiautomator": OSError("device offline")})
    with pytest.raises(AdbUnavailable):
        dev.dump()


def test_tap_and_swipe_send_the_expected_input_events():
    dev, runner = _device({})
    dev.tap(855, 315)
    dev.swipe(450, 850, 450, 550, 1200)
    joined = [" ".join(c) for c in runner.calls]
    assert any("input tap 855 315" in c for c in joined)
    assert any("input swipe 450 850 450 550 1200" in c for c in joined)


def test_it_satisfies_the_sweep_protocol():
    """`app_sweep.sweep` takes anything with these four methods. If this
    drifts, the sweep breaks only on a real device, which is the most
    expensive place to find out."""
    from beer_in_this_town.app_sweep import Device

    dev, _ = _device({})
    assert isinstance(dev, Device)


# --- the device GPS, which calibrates the camera --------------------------

LOCATION = """
Location Providers:
    network: Location[network 32.075318,34.808611 acc=1 et=+24s381ms alt=4.0]
    gps: Location[gps 32.075318,34.808611 acc=1 et=+14h43m51s825ms alt=0.0]
    passive: Location[gps 32.075318,34.808611 acc=1 et=+14h43m51s825ms]
"""


def test_location_reads_the_gps_fix():
    dev = AdbDevice("x", run=FakeRunner({"dumpsys location": LOCATION}))
    assert dev.location() == pytest.approx((32.075318, 34.808611))


def test_location_prefers_gps_over_network():
    out = ("    network: Location[network 1.0,2.0 acc=900]\n"
           "    gps: Location[gps 32.075318,34.808611 acc=1]\n")
    dev = AdbDevice("x", run=FakeRunner({"dumpsys location": out}))
    assert dev.location() == pytest.approx((32.075318, 34.808611))


def test_location_falls_back_to_network_when_there_is_no_gps_fix():
    out = "    network: Location[network 32.1,34.8 acc=20]\n"
    dev = AdbDevice("x", run=FakeRunner({"dumpsys location": out}))
    assert dev.location() == pytest.approx((32.1, 34.8))


def test_no_fix_raises_rather_than_inventing_a_place():
    """A default location would calibrate a whole corpus to somewhere real.

    Every venue would get a plausible coordinate in the wrong city, and
    nothing downstream could tell.
    """
    dev = AdbDevice("x", run=FakeRunner({"dumpsys location": "no fixes\n"}))
    with pytest.raises(AdbUnavailable, match="no location"):
        dev.location()


# --- a killed uiautomator is transient, and ended a depth-3 sweep ----------

class SequenceRunner:
    """Replies to successive calls in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, argv, timeout):
        self.calls += 1
        return self.replies.pop(0)


def test_dump_retries_once_when_uiautomator_is_killed():
    runner = SequenceRunner(["Killed\n", DUMP])
    dev = AdbDevice("x", run=runner)
    assert "Lauter" in dev.dump()
    assert runner.calls == 2


def test_dump_gives_up_when_the_kill_repeats():
    """One retry, not a loop: a kill that keeps happening is not transient."""
    runner = SequenceRunner(["Killed\n", "Killed\n", DUMP])
    dev = AdbDevice("x", run=runner)
    with pytest.raises(AdbUnavailable, match="Killed"):
        dev.dump()
    assert runner.calls == 2


def test_dump_does_not_retry_other_garbage():
    """Only the known-transient case is retried. Anything else is news."""
    runner = SequenceRunner(["ERROR: null root node\n", DUMP])
    dev = AdbDevice("x", run=runner)
    with pytest.raises(AdbUnavailable):
        dev.dump()
    assert runner.calls == 1


def test_launch_stops_the_app_before_starting_it():
    """`monkey` alone *resumes* a running app on whatever screen it was on.

    Found live: it brought back a filter panel a crashed sweep had left open,
    and recovery tapped blindly into it. A launch that is meant to be fresh
    has to stop the app first.
    """
    runner = FakeRunner({"force-stop": "", "monkey": ""})
    AdbDevice("x", run=runner).launch("com.untappdllc.app")
    joined = [" ".join(c) for c in runner.calls]
    stop = next(i for i, c in enumerate(joined) if "force-stop" in c)
    start = next(i for i, c in enumerate(joined) if "monkey" in c)
    assert stop < start
    assert "com.untappdllc.app" in joined[stop]
