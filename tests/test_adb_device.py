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
