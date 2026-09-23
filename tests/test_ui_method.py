"""The dashboard offers both sweep methods, and asks for the emulator only
when the person chose the app's map.

`sweep --method search` needs no emulator, so a dashboard that still sent
everyone through BlueStacks setup first would stop a search user at a step
their method never uses.
"""
import pytest

from beer_in_this_town.state import last_intent
from beer_in_this_town.ui import flow
from beer_in_this_town.ui.checks import OK, UNKNOWN, Check, next_step

from .test_city_handoff import _post, board  # noqa: F401  (fixture)

ROWS = (
    Check("chrome", "Chrome", OK, "ok", verified=True),
    Check("playwright", "Browser automation", OK, "ok", verified=True),
    Check("emulator", "Android emulator", UNKNOWN, "Not checked yet."),
    Check("google", "Google account", OK, "ok", verified=True),
    Check("untappd", "Untappd account", OK, "ok", verified=True),
)


@pytest.mark.unit
def test_a_search_city_goes_to_build_without_the_emulator():
    step = next_step(ROWS, intent={"query": "Tel Aviv", "method": "search"})
    assert step.key == "build"


@pytest.mark.unit
def test_a_map_city_still_needs_the_emulator_first():
    step = next_step(ROWS, intent={"query": "Tel Aviv", "method": "map"})
    assert step.key == "emulator"


@pytest.mark.unit
def test_no_choice_yet_follows_the_default_method():
    # Default is search: nobody is sent to BlueStacks before choosing.
    assert next_step(ROWS).key == "city"


@pytest.mark.unit
@pytest.mark.parametrize("method", ["search", "map"])
def test_the_sweep_command_names_the_method(method):
    sweep = flow.build_card("Tel Aviv", method=method)["stages"][0]
    assert f"--method {method}" in sweep["command"]


@pytest.mark.unit
def test_a_search_sweep_is_explained_as_a_search_not_a_map_pan():
    sweep = flow.build_card("Tel Aviv", method="search")["stages"][0]
    assert "search" in sweep["title"].lower()
    assert "pans" not in sweep["explain"].lower()
    assert "precondition" not in sweep, "no BlueStacks step for a search"


@pytest.mark.unit
def test_a_map_sweep_keeps_its_emulator_precondition():
    sweep = flow.build_card("Tel Aviv", method="map")["stages"][0]
    assert "BlueStacks" in sweep["precondition"]


@pytest.mark.unit
def test_the_page_can_save_a_method_with_the_city(board):  # noqa: F811
    base, token = board
    code, body = _post(base, "/api/city", token,
                       {"city": "london", "method": "map"})
    assert code == 200
    assert body["intent"]["method"] == "map"
    assert last_intent()["method"] == "map"


@pytest.mark.unit
def test_an_unknown_method_over_the_wire_is_a_clean_400(board):  # noqa: F811
    base, token = board
    code, body = _post(base, "/api/city", token,
                       {"city": "london", "method": "teleport"})
    assert code == 400
    assert body["error"] == "bad_city"
    assert last_intent() is None
