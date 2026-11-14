"""The emulator checks: six questions, in order, that never raise.

A fake runner stands in for adb. No device, no network.
"""
from __future__ import annotations

import subprocess

import pytest

from beer_in_this_town.emulator_checks import (
    CHECK_NAMES,
    MAP_PACKAGE,
    check_emulator,
    emulator_ready,
    first_failure,
)

pytestmark = pytest.mark.unit

SERIAL = "127.0.0.1:5555"
DEVICES = f"List of devices attached\n{SERIAL}\tdevice\n\n"
PACKAGES = f"package:{MAP_PACKAGE}\n"
SIZE = "Physical size: 900x1600\n"
SDK = "28\n"
APP = ("    versionCode=2147102969 minSdk=26 targetSdk=36\n"
       "    versionName=5.0.3\n")


def runner(devices=DEVICES, packages=PACKAGES, size=SIZE, sdk=SDK, app=APP,
           codes=None, raises=None):
    """A fake `adb`: answers by subcommand, records what it was asked."""
    codes = codes or {}
    calls: list[list[str]] = []

    def run(argv, timeout):
        calls.append(argv)
        assert timeout <= 10, "status must stay cheap"
        key = ("devices" if argv[1:] == ["devices"]
               else "pm" if "pm" in argv else "wm" if "wm" in argv
               else "getprop" if "getprop" in argv
               else "dumpsys" if "dumpsys" in argv else "?")
        if raises and key in raises:
            raise raises[key]
        out = {"devices": devices, "pm": packages, "wm": size,
               "getprop": sdk, "dumpsys": app}[key]
        return codes.get(key, 0), out

    run.calls = calls
    return run


def have_adb(name):
    return "C:/platform-tools/adb.exe"


def no_adb(name):
    return None


def test_a_ready_emulator_passes_every_check_in_order():
    checks = check_emulator(SERIAL, run=runner(), which=have_adb)
    assert [c.name for c in checks] == list(CHECK_NAMES)
    assert all(c.ok for c in checks), checks
    assert emulator_ready(checks)
    assert first_failure(checks) is None


def test_no_adb_fails_first_and_skips_the_rest_without_calling_it():
    run = runner()
    checks = check_emulator(SERIAL, run=run, which=no_adb)
    assert [c.name for c in checks] == list(CHECK_NAMES)
    assert not checks[0].ok and "platform-tools" in checks[0].remedy
    assert all(not c.ok and "not checked" in c.detail for c in checks[1:])
    assert run.calls == [], "nothing may be run when adb is not there"
    assert not emulator_ready(checks)


def test_a_missing_device_names_adb_connect_with_the_serial():
    checks = check_emulator(SERIAL, which=have_adb,
                            run=runner(devices="List of devices attached\n\n"))
    bad = first_failure(checks)
    assert bad.name == "device_connected"
    assert f"adb connect {SERIAL}" in bad.remedy
    assert "Android Debug Bridge" in bad.remedy


def test_an_unauthorized_device_is_not_reported_as_missing():
    devices = f"List of devices attached\n{SERIAL}\tunauthorized\n"
    bad = first_failure(check_emulator(SERIAL, which=have_adb,
                                       run=runner(devices=devices)))
    assert bad.name == "device_connected"
    assert "unauthorized" in bad.detail


def test_a_different_device_is_not_mistaken_for_ours():
    devices = "List of devices attached\nemulator-5554\tdevice\n"
    bad = first_failure(check_emulator(SERIAL, which=have_adb,
                                       run=runner(devices=devices)))
    assert bad.name == "device_connected"
    assert "emulator-5554" in bad.detail
    assert "BEERTOWN_ADB_SERIAL" in bad.remedy


def test_the_daemon_banner_is_not_a_device():
    devices = ("* daemon not running; starting now at tcp:5037\n"
               "* daemon started successfully\n"
               f"List of devices attached\n{SERIAL}\tdevice\n")
    checks = check_emulator(SERIAL, which=have_adb, run=runner(devices=devices))
    assert emulator_ready(checks)


def test_untappd_missing_says_how_to_install_it():
    bad = first_failure(check_emulator(SERIAL, which=have_adb,
                                       run=runner(packages="")))
    assert bad.name == "untappd_installed"
    assert "Play Store" in bad.remedy


def test_a_package_that_merely_starts_with_the_name_does_not_count():
    bad = first_failure(check_emulator(
        SERIAL, which=have_adb,
        run=runner(packages=f"package:{MAP_PACKAGE}.beta\n")))
    assert bad is not None and bad.name == "untappd_installed"


@pytest.mark.parametrize("size", ["Physical size: 1080x1920\n",
                                  "Physical size: 1920x1080\n"])
def test_the_wrong_screen_size_is_refused(size):
    bad = first_failure(check_emulator(SERIAL, which=have_adb,
                                       run=runner(size=size)))
    assert bad.name == "screen_size"
    assert "900x1600" in bad.remedy


def test_stock_landscape_bluestacks_is_accepted():
    """BlueStacks' default 1600x900 draws the portrait Untappd app at
    900x1600 -- the size the sweep is calibrated for. Refusing it failed a
    working setup on the first live run."""
    checks = check_emulator(SERIAL, which=have_adb,
                            run=runner(size="Physical size: 1600x900\n"))
    assert emulator_ready(checks)
    assert "portrait" in checks[-1].detail


def test_an_override_size_wins_over_the_physical_one():
    size = "Physical size: 1080x1920\nOverride size: 900x1600\n"
    assert emulator_ready(check_emulator(SERIAL, which=have_adb,
                                         run=runner(size=size)))


def test_a_timeout_is_a_failed_check_not_an_exception():
    run = runner(raises={"devices": subprocess.TimeoutExpired("adb", 8)})
    bad = first_failure(check_emulator(SERIAL, which=have_adb, run=run))
    assert bad.name == "device_connected"
    assert "8s" in bad.detail


def test_any_exception_is_a_failed_check_not_an_exception():
    run = runner(raises={"wm": OSError("boom")})
    checks = check_emulator(SERIAL, which=have_adb, run=run)
    assert checks[-1].name == "screen_size" and not checks[-1].ok
    assert "boom" in checks[-1].detail


def test_a_nonzero_exit_is_reported_with_its_output():
    run = runner(codes={"devices": 1}, devices="error: protocol fault")
    bad = first_failure(check_emulator(SERIAL, which=have_adb, run=run))
    assert "protocol fault" in bad.detail


def test_every_adb_call_is_addressed_by_serial_after_devices():
    run = runner()
    check_emulator(SERIAL, which=have_adb, run=run)
    for argv in run.calls[1:]:
        assert argv[:3] == ["adb", "-s", SERIAL]


def test_the_serial_defaults_to_the_settings_one(monkeypatch):
    monkeypatch.delenv("BEERTOWN_ADB_SERIAL", raising=False)
    run = runner()
    assert emulator_ready(check_emulator(which=have_adb, run=run))


def test_the_serial_env_override_is_honoured(monkeypatch):
    monkeypatch.setenv("BEERTOWN_ADB_SERIAL", "emulator-5554")
    devices = "List of devices attached\nemulator-5554\tdevice\n"
    assert emulator_ready(check_emulator(which=have_adb,
                                         run=runner(devices=devices)))


def test_checks_serialise_for_the_envelope():
    checks = check_emulator(SERIAL, which=no_adb, run=runner())
    row = checks[0].to_dict()
    assert set(row) == {"name", "ok", "detail", "remedy"}


def test_an_empty_list_is_not_ready():
    assert emulator_ready([]) is False


# --- Untappd 5 needs Android 8, and the sweep needs Untappd 5 --------------
#
# Found 2026-11-14: the old Nougat (Android 7.1, SDK 25) BlueStacks instance
# never offered the Untappd 5 update, because 5.x needs SDK 26. Nothing said
# so; it cost an hour. And the sweep now drives only 5.x's screens, so a 4.x
# app would fail minutes in as `app_screen_unexpected`.

def test_the_order_puts_the_android_check_before_the_app_ones():
    assert list(CHECK_NAMES) == ["adb_on_path", "device_connected",
                                 "android_version", "untappd_installed",
                                 "untappd_version", "screen_size"]


def test_android_7_is_refused_with_a_way_out():
    checks = check_emulator(SERIAL, which=have_adb, run=runner(sdk="25\n"))
    bad = first_failure(checks)
    assert bad.name == "android_version"
    assert "25" in bad.detail
    assert "Pie" in bad.remedy and "64-bit" in bad.remedy
    later = checks[CHECK_NAMES.index("untappd_installed"):]
    assert all("not checked" in c.detail for c in later)


def test_android_8_is_enough():
    checks = check_emulator(SERIAL, which=have_adb, run=runner(sdk="26\n"))
    assert emulator_ready(checks), checks


def test_an_unreadable_android_version_is_a_failure_not_a_pass():
    checks = check_emulator(SERIAL, which=have_adb, run=runner(sdk="\n"))
    assert first_failure(checks).name == "android_version"


def test_untappd_4_is_refused_with_how_to_update():
    old = "    versionName=4.6.12\n"
    checks = check_emulator(SERIAL, which=have_adb, run=runner(app=old))
    bad = first_failure(checks)
    assert bad.name == "untappd_version"
    assert "4.6.12" in bad.detail
    assert "Play Store" in bad.remedy


def test_an_unreadable_untappd_version_is_a_failure_not_a_pass():
    checks = check_emulator(SERIAL, which=have_adb,
                            run=runner(app="nothing\n"))
    assert first_failure(checks).name == "untappd_version"


def test_a_later_untappd_major_still_passes():
    checks = check_emulator(SERIAL, which=have_adb,
                            run=runner(app="    versionName=6.1\n"))
    assert emulator_ready(checks), checks
