"""The city a person types has to reach the agent, or the two disagree.

The wizard asked for a city, kept it in `localStorage`, and printed a command.
An agent reading `status` carried on offering the built-in default. Both halves
of the setup behaved correctly and produced different answers, with nothing
anywhere able to see that they had.

No network, no browser.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from beer_in_this_town.config import Settings
from beer_in_this_town.state import (
    BadIntent,
    inspect_state,
    last_intent,
    next_actions,
    record_intent,
    record_run,
)
from beer_in_this_town.ui import server as srv


@pytest.fixture
def ready(tmp_path):
    """A machine past the sign-in, so `next_actions` reaches the run."""
    from beer_in_this_town.state import record_verification

    s = Settings(profile_dir=tmp_path / "profile",
                 storage_state=tmp_path / "storage_state.json")
    s.storage_state.write_text("{}", encoding="utf-8")
    record_verification({"google": {"ok": True}, "untappd": {"ok": True}},
                        ok=True)
    return s


# --- the handoff ----------------------------------------------------------

@pytest.mark.unit
def test_the_city_reaches_the_agents_next_action(ready):
    """The whole point. Type London, and the agent is offered London."""
    before = next_actions(inspect_state(ready), ready)
    assert any("singapore" in a for a in before), "expected the default first"

    record_intent("london")

    after = next_actions(inspect_state(ready), ready)
    assert any('--query "london"' in a for a in after), \
        "the city never reached the agent"
    assert not any("singapore" in a for a in after)


@pytest.mark.unit
def test_the_list_name_follows_the_city(ready):
    """Leaving the title behind is the bug `record_run` already documents:
    a London CSV aimed at a Singapore list, which is a write to a live
    account."""
    record_intent("london")
    state = inspect_state(ready)
    assert state["last_run"]["map_title"] == "London Bars"


@pytest.mark.unit
def test_an_intention_is_not_a_run(ready):
    """`recorded` means a run happened. Writing the city into `last_run.json`
    would have claimed one had."""
    record_intent("london")
    state = inspect_state(ready)
    assert state["last_run"]["recorded"] is False
    assert state["intent"]["query"] == "london"


@pytest.mark.unit
def test_a_real_run_outranks_an_intention(ready, tmp_path):
    """Once something has actually run, that is the truth about this corpus."""
    record_intent("london")
    record_run(query="berlin", map_title="Berlin Bars",
               csv_path=tmp_path / "venues_berlin.csv")

    state = inspect_state(ready)
    assert state["last_run"]["query"] == "berlin"
    assert state["intent"]["query"] == "london", "the intention was destroyed"


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_an_empty_city_is_refused(bad):
    with pytest.raises(BadIntent):
        record_intent(bad)


@pytest.mark.unit
def test_an_absurd_city_is_refused():
    with pytest.raises(BadIntent):
        record_intent("x" * 500)


@pytest.mark.unit
def test_whitespace_is_tidied_not_preserved():
    assert record_intent("  new   york  ")["query"] == "new york"


@pytest.mark.unit
def test_a_corrupt_record_is_ignored_rather_than_fatal(tmp_path, monkeypatch):
    from beer_in_this_town import state as st

    monkeypatch.setattr(st, "INTENT", tmp_path / "intent.json")
    st.INTENT.write_text("{not json", encoding="utf-8")
    assert st.last_intent() is None


# --- over the wire --------------------------------------------------------

@pytest.fixture
def board():
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


def _post(base, path, token, body, headers=None):
    r = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                               method="POST")
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("X-Beertown-Token", token)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.mark.unit
def test_the_page_can_save_a_city(board):
    base, token = board
    code, body = _post(base, "/api/city", token, {"city": "london"})
    assert code == 200
    assert body["intent"]["query"] == "london"
    assert last_intent()["query"] == "london"


@pytest.mark.unit
def test_saving_a_city_needs_the_token(board):
    """It changes what an agent will go and scrape. Same door as everything."""
    base, _ = board
    code, _ = _post(base, "/api/city", None, {"city": "london"})
    assert code == 401
    assert last_intent() is None


@pytest.mark.unit
def test_a_cross_site_page_cannot_set_the_city(board):
    base, token = board
    code, _ = _post(base, "/api/city", token, {"city": "london"},
                    headers={"Origin": "https://evil.example.com"})
    assert code == 403
    assert last_intent() is None


@pytest.mark.unit
def test_an_empty_city_over_the_wire_is_a_clean_400(board):
    base, token = board
    code, body = _post(base, "/api/city", token, {"city": "  "})
    assert code == 400
    assert body["error"] == "bad_city"
