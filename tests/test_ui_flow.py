"""The dashboard's v0.2 steps: emulator, build and Google Maps.

These steps are explained, not automated -- the user's rule is "automate
nothing new". So what is tested is the explanation: the exact commands a
person copies, the counts read from the files each stage writes, and that the
server exposes none of it as something it would run. Offline throughout; the
emulator checks are stubbed and no browser is opened.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from beer_in_this_town import config, emulator_checks
from beer_in_this_town.config import Settings
from beer_in_this_town.ui import actions, flow
from beer_in_this_town.ui import server as srv

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project folder whose data/ is empty, with paths shown relative."""
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return tmp_path / "data"


def _csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "name,lat,lng\n" + "".join(f"v{i},1,2\n" for i in range(rows))
    path.write_text(body, encoding="utf-8")


# --- the slug ---------------------------------------------------------------

@pytest.mark.parametrize("city,slug", [
    ("Tel Aviv", "tel-aviv"), ("London", "london"),
    ("St. Louis, MO", "st-louis-mo"), ("  Berlin  ", "berlin"),
])
def test_the_folder_follows_the_contract_slug(city, slug):
    assert flow.city_slug(city) == slug


# --- build the map ------------------------------------------------------------

def test_a_fresh_city_shows_four_commands_and_nothing_done(project):
    card = flow.build_card("Tel Aviv")

    assert card["folder"] == "data/tel-aviv"
    assert [s["key"] for s in card["stages"]] == [
        "sweep", "enrich", "filter", "export"]
    assert not any(s["done"] for s in card["stages"])
    assert card["next"] == "sweep"
    by = {s["key"]: s for s in card["stages"]}
    assert by["sweep"]["command"] == 'beertown sweep --city "Tel Aviv"'
    assert by["enrich"]["command"] == 'beertown enrich --city "Tel Aviv"'
    assert by["filter"]["command"] == 'beertown filter --city "Tel Aviv"'
    assert by["export"]["command"].startswith(
        'beertown export --city "Tel Aviv"')
    assert by["sweep"]["file"] == "data/tel-aviv/1_sweep.csv"


def test_the_sweep_names_its_manual_precondition(project):
    sweep = flow.build_card("Tel Aviv")["stages"][0]
    assert "Discover -> View Map" in sweep["precondition"]
    assert "--here" in sweep["options"]


def test_an_agent_is_told_it_can_run_the_same_commands(project):
    assert "--json" in flow.build_card("Tel Aviv")["agent_note"]


def test_done_and_counts_come_from_the_files(project):
    folder = project / "tel-aviv"
    _csv(folder / "1_sweep.csv", 12)
    _csv(folder / "2_enriched.csv", 10)
    _csv(folder / "3_venues.csv", 7)
    _csv(folder / "3_excluded.csv", 3)
    (folder / "venues.kml").write_text(
        "<kml>" + "<Placemark></Placemark>" * 7 + "</kml>", encoding="utf-8")

    card = flow.build_card("Tel Aviv")
    by = {s["key"]: s for s in card["stages"]}

    assert all(s["done"] for s in card["stages"])
    assert card["next"] is None
    assert (by["sweep"]["count"], by["enrich"]["count"],
            by["filter"]["count"], by["export"]["count"]) == (12, 10, 7, 7)
    assert by["filter"]["excluded"] == 3


def test_a_half_built_city_points_at_the_next_stage(project):
    _csv(project / "tel-aviv" / "1_sweep.csv", 5)
    card = flow.build_card("Tel Aviv")
    assert card["next"] == "enrich"
    assert flow.venues_ready("Tel Aviv") is False


def test_a_count_follows_the_file_as_it_changes(project):
    path = project / "tel-aviv" / "1_sweep.csv"
    _csv(path, 2)
    assert flow.build_card("Tel Aviv")["stages"][0]["count"] == 2
    _csv(path, 40)
    assert flow.build_card("Tel Aviv")["stages"][0]["count"] == 40


def test_no_city_no_commands(project):
    assert flow.build_card(None)["stages"] == []
    assert flow.maps_card(None, None) == {"city": None}


def test_a_quote_in_the_city_cannot_break_the_shown_command(project):
    card = flow.build_card('Tel "Aviv')
    assert card["stages"][0]["command"] == 'beertown sweep --city "Tel Aviv"'


# --- Google Maps --------------------------------------------------------------

def test_the_trial_pin_is_exactly_the_contract_command(project):
    card = flow.maps_card("Tel Aviv", "Tel Aviv Bars")
    assert card["commands"]["pin_trial"] == (
        'beertown pin --csv data/tel-aviv/3_venues.csv '
        '--list "Tel Aviv Bars" --limit 3')
    assert card["commands"]["pin_rest"] == (
        'beertown pin --csv data/tel-aviv/3_venues.csv --list "Tel Aviv Bars"')
    assert card["commands"]["notes_trial"] == (
        'beertown notes --csv data/tel-aviv/3_venues.csv '
        '--list "Tel Aviv Bars" --limit 3')


def test_the_page_can_swap_in_the_list_name_the_person_used(project):
    card = flow.maps_card("Tel Aviv", None)
    assert card["list_default"] == "Tel Aviv Bars"
    for template in card["templates"].values():
        assert flow.LIST_TOKEN in template


def test_the_terms_of_service_are_said_plainly(project):
    tos = flow.maps_card("Tel Aviv", None)["tos"]
    assert "Terms of Service" in tos
    assert "choice" in tos


def test_the_maps_step_knows_whether_there_is_anything_to_pin(project):
    assert flow.maps_card("Tel Aviv", None)["csv_ready"] is False
    _csv(project / "tel-aviv" / "3_venues.csv", 1)
    assert flow.maps_card("Tel Aviv", None)["csv_ready"] is True


# --- the emulator step --------------------------------------------------------

def test_the_emulator_instructions_cover_every_manual_setting():
    card = flow.emulator_card(None)
    text = " ".join(s["title"] + " " + s["body"] + " " + s.get("cmd", "")
                    for s in card["steps"])
    for needed in ("BlueStacks 5", "900 x 1600", "Portrait",
                   "Android Debug Bridge", "adb connect 127.0.0.1:5555",
                   "Play Store", "Sign in to Untappd"):
        assert needed in text, f"the setup never mentions {needed!r}"
    assert "calibrat" in text or "measured" in text, "the size has no why"
    assert card["checked"] is False


def test_the_emulator_action_narrates_each_check(monkeypatch):
    fake = [emulator_checks.Check("adb_on_path", True, "adb found.", ""),
            emulator_checks.Check("device_connected", False, "not connected.",
                                  "Run adb connect 127.0.0.1:5555.")]
    monkeypatch.setattr(emulator_checks, "check_emulator",
                        lambda serial=None: fake)
    said = []
    out = actions.check_emulator(Settings(),
                                 lambda t, aside=False: said.append(t),
                                 lambda k, v: None)

    assert out["emulator"]["ready"] is False
    assert [c["name"] for c in out["emulator"]["checks"]] == [
        "adb_on_path", "device_connected"]
    assert any("adb connect" in t for t in said), "the remedy was not shown"


def test_a_raising_check_is_reported_not_swallowed(monkeypatch):
    def boom(serial=None):
        raise RuntimeError("adb exploded")

    monkeypatch.setattr(emulator_checks, "check_emulator", boom)
    out = actions.check_emulator(Settings(), lambda *a, **k: None,
                                 lambda k, v: None)
    assert out["emulator"]["ready"] is False
    assert "adb exploded" in out["emulator"]["error"]


def test_the_stand_in_checks_never_raise_without_adb(monkeypatch):
    monkeypatch.setattr(emulator_checks.shutil, "which", lambda name: None)
    found = emulator_checks.check_emulator()
    assert [c.name for c in found] == [
        "adb_on_path", "device_connected", "untappd_installed", "screen_size"]
    assert not found[0].ok and found[0].remedy
    assert emulator_checks.emulator_ready(found) is False


def test_once_swept_an_unchecked_emulator_stops_sending_you_back(tmp_path):
    """A restarted dashboard forgets the emulator check. After the sweep the
    emulator is not needed, so step 2 must not reclaim the person."""
    from beer_in_this_town.ui.checks import OK, UNKNOWN, Check, next_step

    rows = (
        Check("chrome", "Chrome", OK, "ok", verified=True),
        Check("playwright", "Browser automation", OK, "ok", verified=True),
        Check("emulator", "Android emulator", UNKNOWN, "Not checked yet."),
        Check("google", "Google account", OK, "ok", verified=True),
        Check("untappd", "Untappd account", OK, "ok", verified=True),
    )
    intent = {"query": "Tel Aviv"}
    assert next_step(rows, intent=intent).key == "emulator"
    assert next_step(rows, intent=intent, swept=True).key == "build"
    assert next_step(rows, intent=intent, swept=True,
                     venues_ready=True).key == "maps"


# --- over the wire ------------------------------------------------------------

@pytest.fixture
def board(project):
    httpd, url = srv.build(Settings(), port=0)
    token = url.split("#", 1)[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", token
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _req(base, path, token, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    r.add_header("X-Beertown-Token", token)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _wait_for_job(base, token):
    for _ in range(50):
        _, state = _req(base, "/api/state", token)
        if state["job"] and state["job"]["state"] != "running":
            return state
        threading.Event().wait(0.1)
    raise AssertionError("the job never finished")


def test_the_state_carries_every_step_card(board):
    base, token = board
    code, state = _req(base, "/api/state", token)
    assert code == 200
    assert [s["key"] for s in state["stages"]] == [
        "ready", "emulator", "accounts", "city", "build", "maps"]
    assert set(state["cards"]) >= {"emulator", "accounts", "city", "build",
                                   "maps"}
    assert "emulator" in [c["key"] for c in state["checks"]]


def test_check_again_runs_the_emulator_checks_and_shows_them(board,
                                                             monkeypatch):
    fake = [emulator_checks.Check(n, True, "fine", "") for n in
            ("adb_on_path", "device_connected", "untappd_installed",
             "screen_size")]
    monkeypatch.setattr(emulator_checks, "check_emulator",
                        lambda serial=None: fake)
    base, token = board
    code, _ = _req(base, "/api/run", token, "POST", {"action": "emulator"})
    assert code == 202

    state = _wait_for_job(base, token)
    card = state["cards"]["emulator"]
    assert card["checked"] and card["ready"]
    assert len(card["checks"]) == 4
    row = next(c for c in state["checks"] if c["key"] == "emulator")
    assert row["state"] == "ok"


def test_a_chosen_city_reaches_the_build_and_maps_cards(board):
    base, token = board
    _req(base, "/api/city", token, "POST", {"city": "Tel Aviv"})
    _, state = _req(base, "/api/state", token)

    build = state["cards"]["build"]
    assert build["city"] == "Tel Aviv"
    assert build["stages"][0]["command"] == 'beertown sweep --city "Tel Aviv"'
    assert state["cards"]["maps"]["commands"]["pin_trial"].endswith(
        '--list "Tel Aviv Bars" --limit 3')


@pytest.mark.parametrize("action", ["pin", "notes", "sweep", "closures",
                                    "enrich", "filter", "export"])
def test_the_commands_are_shown_never_run(board, action):
    """Showing a command is not permission to run it from here."""
    base, token = board
    code, body = _req(base, "/api/run", token, "POST", {"action": action})
    assert code == 400
    assert body["error"] == "unknown_action"


def test_the_page_has_no_trace_of_the_removed_flow():
    page = (flow.Path(__file__).parent.parent / "beer_in_this_town" / "ui"
            / "index.html").read_text(encoding="utf-8")
    for gone in ("My Maps", "--no-upload", "beertown run", "search stops"):
        assert gone not in page, f"the page still mentions {gone!r}"
    for view in ("emulator(", "build(", "maps(", "Copy"):
        assert view in page
