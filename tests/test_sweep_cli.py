"""The `sweep` and `doctor` commands, and what 0.2.0 removed from the CLI.

No emulator: the census is replaced and the emulator checks are injected.
"""
from __future__ import annotations

import json

import pytest

from beer_in_this_town import cli, state
from beer_in_this_town.app_calibrate import Calibration
from beer_in_this_town.app_pipeline import Census
from beer_in_this_town.app_sweep import SweepResult
from beer_in_this_town.config import Settings, city_slug, stage_path
from beer_in_this_town.emulator_checks import Check
from beer_in_this_town.models import Venue, VenueRef

pytestmark = pytest.mark.unit

NAMES = ("adb_on_path", "device_connected", "untappd_installed", "screen_size")


def ready(serial=None):
    return [Check(n, True, "ok", "") for n in NAMES]


def unplugged(serial=None):
    return [Check("adb_on_path", True, "adb", ""),
            Check("device_connected", False, "127.0.0.1:5555 is not listed",
                  "Run `adb connect 127.0.0.1:5555`."),
            Check("untappd_installed", False, "not checked", "Fix it first."),
            Check("screen_size", False, "not checked", "Fix it first.")]


def fake_census(device, city, s, **kw):
    fake_census.kw = kw
    venues = [Venue(ref=VenueRef(venue_id="", slug="", name=n, category=None,
                                 address=None, city=city),
                    total=None, unique=None, monthly=None, you=None,
                    lat=32.08, lng=34.78)
              for n in ("Beer Bazaar", "Porter & Sons")]
    cal = Calibration(scale=1.0, rotation_deg=0.0, shift_m=(0.0, 0.0),
                      median_residual_m=18.0, inliers=("Beer Bazaar",),
                      dropped=())
    return Census(venues=venues, sweep=SweepResult(cells_visited=5),
                  calibration=cal, centre=(32.08, 34.78),
                  centre_source="geocoder", osm_radius_km=3.0)


# --- sweep ------------------------------------------------------------------

def test_sweep_refuses_before_touching_the_device_when_the_emulator_is_not_ready(
        monkeypatch):
    monkeypatch.setattr(cli, "census",
                        lambda *a, **k: pytest.fail("census ran"))
    s = Settings(query="Tel Aviv", map_title="TA")
    env = cli.cmd_sweep(s, here=False, min_depth=1, max_depth=3, formats=(),
                        device=object(), emulator=unplugged)
    assert env.ok is False
    assert env.error.code == "emulator_unavailable"
    assert "adb connect 127.0.0.1:5555" in env.error.remedy
    assert "device_connected" in env.error.message
    assert [c["name"] for c in env.data["emulator"]] == list(NAMES)
    assert not stage_path("Tel Aviv", "1_sweep.csv").exists()


def test_sweep_writes_the_stage_file_and_offers_enrich(monkeypatch):
    monkeypatch.setattr(cli, "census", fake_census)
    s = Settings(query="Tel Aviv", map_title="TA Beer")
    env = cli.cmd_sweep(s, here=False, min_depth=1, max_depth=3,
                        formats=("kml",), device=object(), emulator=ready)
    assert env.ok, env.error
    out = stage_path("Tel Aviv", "1_sweep.csv")
    assert out.exists() and env.data["csv"] == str(out)
    assert out.parent.name == "tel-aviv"
    assert env.data["maps"]["kml"].endswith("1_sweep.kml")
    assert env.next_actions == [
        'python -m beer_in_this_town enrich --city "Tel Aviv" --json']
    assert not any(" pin " in f" {h} " for h in env.next_actions)
    # status now knows the city and list
    assert json.loads(state.LAST_RUN.read_text())["query"] == "Tel Aviv"


def test_sweep_rewrites_its_output_on_a_rerun(monkeypatch):
    monkeypatch.setattr(cli, "census", fake_census)
    s = Settings(query="Tel Aviv")
    out = stage_path("Tel Aviv", "1_sweep.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("stale\n", encoding="utf-8")
    cli.cmd_sweep(s, here=False, min_depth=1, max_depth=3, formats=(),
                  device=object(), emulator=ready)
    assert "Beer Bazaar" in out.read_text(encoding="utf-8-sig")


def test_the_cli_defaults_are_the_measured_ones(monkeypatch):
    seen = {}

    def capture(s, **kw):
        seen.update(kw)
        return cli.Envelope(command="sweep", ok=True)

    monkeypatch.setattr(cli, "cmd_sweep", capture)
    assert cli.main(["sweep", "--city", "Tel Aviv", "--method", "map",
                     "--json"]) == 0
    assert (seen["min_depth"], seen["max_depth"]) == (1, 3)
    assert seen["formats"] == ()


def test_search_is_the_default_method_and_collects_the_top_1000(monkeypatch):
    seen = {}

    def capture(s, city, **kw):
        seen.update(kw, city=city)
        return cli.Envelope(command="sweep", ok=True)

    monkeypatch.setattr(cli, "cmd_search_sweep", capture)
    monkeypatch.setattr(cli, "cmd_sweep", lambda *a, **k: pytest.fail(
        "the default sweep drove the emulator"))
    assert cli.main(["sweep", "--city", "Tel Aviv", "--json"]) == 0
    assert seen == {**seen, "city": "Tel Aviv", "top": 1000, "formats": ()}


@pytest.mark.parametrize("extra", [["--here"], ["--top", "0"],
                                   ["--top", "1001"]])
def test_search_refuses_what_it_cannot_do(extra, monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_search_sweep", lambda *a, **k: pytest.fail(
        "searched despite a bad argument"))
    code = cli.main(["sweep", "--city", "x", "--method", "search", *extra,
                     "--json"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] ==         "bad_arguments"


def test_the_census_is_asked_for_the_depths_it_was_given(monkeypatch):
    monkeypatch.setattr(cli, "census", fake_census)
    cli.cmd_sweep(Settings(query="x"), here=True, min_depth=2, max_depth=4,
                  formats=(), device=object(), emulator=ready)
    assert fake_census.kw == {"here": True, "min_depth": 2, "max_depth": 4,
                              "fresh": False}


def test_an_emulator_failure_reaches_the_envelope_via_main(monkeypatch, capsys):
    monkeypatch.setattr(cli, "check_emulator", unplugged)
    code = cli.main(["sweep", "--city", "Tel Aviv", "--method", "map",
                     "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["error"]["code"] == "emulator_unavailable"


@pytest.mark.parametrize("argv,code", [
    (["sweep", "--city", "x", "--format", "pdf", "--json"], "bad_format"),
    (["sweep", "--city", "x", "--min-depth", "3", "--max-depth", "1",
      "--json"], "bad_arguments"),
])
def test_bad_sweep_arguments_are_envelopes(argv, code, capsys):
    assert cli.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == code


# --- doctor -----------------------------------------------------------------

def test_doctor_reports_every_emulator_check_and_the_first_fix():
    env = cli.cmd_doctor(Settings(), emulator=unplugged)
    assert [c["name"] for c in env.data["emulator"]] == list(NAMES)
    assert env.data["emulator_ready"] is False
    assert env.ok is False
    assert any("adb connect" in w for w in env.warnings)


def test_doctor_with_a_ready_emulator_reminds_about_the_map_screen():
    env = cli.cmd_doctor(Settings(), emulator=ready)
    assert env.data["emulator_ready"] is True
    assert any("Discover -> View Map" in h for h in env.hints)


# --- what 0.2.0 removed -------------------------------------------------------

@pytest.mark.parametrize("gone", ["cmd_run", "_probe_search", "upload_kml",
                                  "collect_venue_refs"])
def test_the_web_search_and_upload_paths_are_gone(gone):
    assert not hasattr(cli, gone)


def test_run_is_not_a_subcommand(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--json"])
    # EnvelopeParser reads --json from sys.argv, so this is the text form.
    assert "invalid choice: 'run'" in capsys.readouterr().out


def test_the_flow_commands_are_registered():
    parser = cli.build_parser()
    for cmd in ("sweep", "enrich", "filter", "export"):
        assert parser.parse_args([cmd, "--city", "x"]).cmd == cmd
        assert cmd in cli._SUBCOMMANDS
    assert "run" not in cli._SUBCOMMANDS


def test_selfcheck_has_no_search_flag():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["selfcheck", "--skip-search"])


# --- config helpers -----------------------------------------------------------

@pytest.mark.parametrize("city,slug", [("Tel Aviv", "tel-aviv"),
                                       ("São Paulo", "sao-paulo"),
                                       ("../../etc", "etc")])
def test_city_slug(city, slug):
    assert city_slug(city) == slug


def test_stage_path_is_under_the_city_folder():
    p = stage_path("Tel Aviv", "3_venues.csv")
    assert p.parent.name == "tel-aviv" and p.name == "3_venues.csv"
