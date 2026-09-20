"""The dashboard can capture a Google session, so its front door is the test.

"It only listens on localhost" is not a boundary. Every page in the user's
browser can send a request to 127.0.0.1, and a server that can open Chrome on
their profile and write `storage_state.json` is worth attacking. Each guard
below is tested from the outside, over a real socket, because a guard that is
present in the source and not in the request path is the failure mode this
repo already has five instances of.

The last test is the one that matters most: there is no route here that can
pin or write notes, whatever else goes wrong.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from beer_in_this_town.config import Settings
from beer_in_this_town.ui import server as srv


@pytest.fixture
def board():
    """A dashboard on a real ephemeral port, torn down after the test."""
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


def _req(base, path, *, token=None, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("X-Beertown-Token", token)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


# --- the token ------------------------------------------------------------

@pytest.mark.unit
def test_reads_need_the_token_too(board):
    """Not just writes. The state reply names the user's profile paths."""
    base, _ = board
    code, body = _req(base, "/api/state")
    assert code == 401
    assert body["error"] == "bad_token"


@pytest.mark.unit
def test_a_wrong_token_is_refused(board):
    base, _ = board
    code, _ = _req(base, "/api/state", token="not-the-token")
    assert code == 401


@pytest.mark.unit
def test_the_right_token_works(board):
    base, token = board
    code, body = _req(base, "/api/state", token=token)
    assert code == 200
    assert body["ok"] is True
    assert [c["key"] for c in body["checks"]][:2] == ["chrome", "playwright"]


@pytest.mark.unit
def test_the_token_is_not_in_the_query_string(board):
    """It rides in the fragment. A query string lands in logs and referrers."""
    _, token = board
    httpd, url = srv.build(Settings(), port=0)
    try:
        assert "?" not in url
        assert url.endswith("#" + url.split("#", 1)[1])
    finally:
        httpd.server_close()


# --- rebinding and cross-site --------------------------------------------

@pytest.mark.unit
def test_a_foreign_host_header_is_refused(board):
    """DNS rebinding: the name resolves here, but the Host header is theirs."""
    base, token = board
    code, body = _req(base, "/api/state", token=token,
                      headers={"Host": "evil.example.com"})
    assert code == 403
    assert body["error"] == "bad_host"


@pytest.mark.unit
def test_a_cross_site_post_is_refused(board):
    """A page on another origin trying to start a sign-in."""
    base, token = board
    code, body = _req(base, "/api/run", token=token, method="POST",
                      body={"action": "connect"},
                      headers={"Origin": "https://evil.example.com"})
    assert code == 403
    assert body["error"] == "bad_origin"


@pytest.mark.unit
def test_our_own_origin_is_fine(board):
    base, token = board
    code, _ = _req(base, "/api/run", token=token, method="POST",
                   body={"action": "nope"},
                   headers={"Origin": "http://127.0.0.1:8765"})
    assert code == 400  # rejected for the action, not the origin


@pytest.mark.unit
@pytest.mark.parametrize("header,ok", [
    ("127.0.0.1:8765", True), ("localhost:8765", True), ("localhost", True),
    ("evil.com", False), ("127.0.0.1.evil.com", False), (None, False),
    ("", False),
])
def test_host_allow_list(header, ok):
    assert srv.host_allowed(header) is ok


@pytest.mark.unit
@pytest.mark.parametrize("origin,ok", [
    (None, True), ("http://127.0.0.1:8765", True), ("http://localhost:1", True),
    ("https://evil.com", False), ("null", False),
])
def test_origin_rule(origin, ok):
    assert srv.origin_allowed(origin) is ok


# --- what the server will and will not do --------------------------------

@pytest.mark.unit
def test_there_is_no_route_that_writes_to_an_account(board):
    """The strongest guard is that the capability is absent.

    If this ever fails, every other test in this file stops being enough.
    """
    base, token = board
    for action in ("pin", "notes", "run", "bootstrap"):
        code, body = _req(base, "/api/run", token=token, method="POST",
                          body={"action": action})
        assert code == 400, f"{action} was accepted by the dashboard"
        assert body["error"] == "unknown_action"


@pytest.mark.unit
def test_unknown_paths_are_refused(board):
    base, token = board
    code, _ = _req(base, "/api/secrets", token=token)
    assert code == 404


@pytest.mark.unit
def test_the_page_itself_needs_no_token_but_carries_a_strict_policy(board):
    """The page has to load before it can know the token.

    It is inert on its own -- the CSP stops it fetching anything off-machine,
    and it holds no data until an authenticated call succeeds.
    """
    base, _ = board
    r = urllib.request.Request(base + "/")
    with urllib.request.urlopen(r, timeout=10) as resp:
        assert resp.status == 200
        csp = resp.headers["Content-Security-Policy"]
        body = resp.read().decode("utf-8")
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "X-Beertown-Token" in body


@pytest.mark.unit
def test_only_one_job_runs_at_a_time(board, monkeypatch):
    """Two sign-ins sharing one Chrome profile can corrupt it."""
    gate = threading.Event()
    monkeypatch.setitem(srv.ACTIONS, "slow",
                        ("Slow thing", lambda s, say: gate.wait(10) or {}))
    base, token = board
    try:
        first, _ = _req(base, "/api/run", token=token, method="POST",
                        body={"action": "slow"})
        second, body = _req(base, "/api/run", token=token, method="POST",
                            body={"action": "slow"})
        assert first == 202
        assert second == 409
        assert body["error"] == "already_running"
    finally:
        gate.set()


@pytest.mark.unit
def test_a_failing_action_reports_why_instead_of_vanishing(board, monkeypatch):
    """`Runner` catches everything, so the message has to survive the catch.

    An action that raises and leaves the page showing nothing is the same
    swallowed-exception shape this repo keeps finding, wearing a thread.
    """
    def boom(s, say):
        say("starting")
        raise RuntimeError("Could not find Google Chrome.")

    monkeypatch.setitem(srv.ACTIONS, "boom", ("Doing a thing", boom))
    base, token = board
    code, _ = _req(base, "/api/run", token=token, method="POST",
                   body={"action": "boom"})
    assert code == 202

    for _ in range(50):
        _, state = _req(base, "/api/state", token=token)
        if state["job"] and state["job"]["state"] != "running":
            break
        threading.Event().wait(0.1)

    job = state["job"]
    assert job["state"] == "failed"
    assert "Could not find Google Chrome." in job["error"]
    assert [s["text"] for s in job["steps"]] == ["starting"]
    assert state["busy"] is False


# --- starting it, and failing to ------------------------------------------

@pytest.mark.unit
def test_a_dashboard_that_will_not_start_says_so_usefully(monkeypatch):
    """`unexpected_error` says "re-run with -v", which cannot help here."""
    from beer_in_this_town import cli

    def wont(s, port):
        raise RuntimeError("Port 8765 is already in use.")

    monkeypatch.setattr("beer_in_this_town.ui.serve_detached", wont)
    env = cli.cmd_ui(Settings(), 8765, open_browser=False, detach=True)

    assert env.ok is False
    assert env.error.code == "port_unavailable"
    assert "--port" in env.error.remedy


@pytest.mark.unit
def test_a_process_that_cannot_be_spawned_is_not_an_unexpected_error(monkeypatch):
    """`Popen` raises OSError, and RuntimeError-only let it fall through.

    Both mean "the dashboard did not start" and want the same remedy; only
    one of them used to get it.
    """
    from beer_in_this_town import cli

    def wont(s, port):
        raise OSError("[WinError 2] The system cannot find the file specified")

    monkeypatch.setattr("beer_in_this_town.ui.serve_detached", wont)
    env = cli.cmd_ui(Settings(), 8765, open_browser=False, detach=True)

    assert env.ok is False
    assert env.error.code == "port_unavailable"


@pytest.mark.unit
def test_a_running_dashboard_is_reused_rather_than_duplicated(monkeypatch):
    """A second --detach must not start a rival on the same profile."""
    from beer_in_this_town import cli

    monkeypatch.setattr(
        "beer_in_this_town.ui.serve_detached",
        lambda s, port: {"url": "http://127.0.0.1:8765/#k", "pid": 7,
                         "port": 8765, "started": False})
    env = cli.cmd_ui(Settings(), 8765, open_browser=False, detach=True)

    assert env.ok is True
    assert env.data["started"] is False
    assert "already running" in " ".join(env.hints)
    assert env.next_actions == [], "the loop was handed another launch"


@pytest.mark.unit
def test_the_record_survives_being_half_written(tmp_path, monkeypatch):
    """The parent polls this file while the child writes it.

    A torn read must cost one more poll, not an exception out of the wait
    loop. The write is atomic now, so this is the belt to that braces.
    """
    from beer_in_this_town.ui import server

    monkeypatch.setattr(server, "RUNNING", tmp_path / "ui.json")
    server.RUNNING.write_text('{"url": "http://127.0.0', encoding="utf-8")
    assert server._read_record() is None

    server.RUNNING.write_text("[]", encoding="utf-8")
    assert server._read_record() is None, "a JSON array was accepted as a record"


@pytest.mark.unit
def test_the_record_is_written_atomically(tmp_path, monkeypatch):
    """No temp file left behind, and the record lands complete."""
    from beer_in_this_town.ui import server

    monkeypatch.setattr(server, "RUNNING", tmp_path / "ui.json")
    server._record("http://127.0.0.1:8765/#tok", 8765)

    assert json.loads(server.RUNNING.read_text())["port"] == 8765
    assert list(tmp_path.glob("*.tmp")) == [], "a temp file was left behind"
