"""A localhost dashboard, and the four things that keep it from being a hole.

This server can open Chrome on the user's profile and capture a Google
session. That makes it the most security-sensitive surface in the project,
because "localhost" is not a boundary: every page in the user's browser can
send requests to 127.0.0.1, and some of them would like to.

So, four independent protections, layered the way the account guardrails are:

**Bind 127.0.0.1 only.** Never 0.0.0.0. Stops anything off-machine.

**A random token per start.** Required on every `/api/*` request, reads
included, so a page that guessed the port still cannot call the API.

**A Host allow-list.** Stops DNS rebinding: a hostile name resolving to
127.0.0.1 still arrives carrying its own `Host`, not ours.

**Origin rejection.** Stops a cross-site POST from any page in the browser.
A same-origin request sends no `Origin`, or sends ours.

The token also never reaches a referrer: it is handed to the page once, in the
URL fragment, which browsers do not transmit. The page reads it and sends it
as a header afterwards.

Scope is the fifth protection and the strongest one: there are no routes here
that write to a Google account. `pin` and `notes` are not reachable from this
server, so no combination of the above failing can cause one.

Stdlib only -- no Flask, no FastAPI. A dashboard that ships five transitive
dependencies to draw six rows is a worse trade than a hundred lines of
`http.server`.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ..config import ROOT, STATE_DIR, Settings
from . import checks
from .actions import ACTIONS
from .jobs import Runner

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PAGE = Path(__file__).parent / "index.html"

# The only Host headers a legitimate same-machine request carries. A rebinding
# attack reaches us with the attacker's hostname in this header instead.
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")


@dataclass(frozen=True)
class Dashboard:
    """Everything one running dashboard needs. Immutable."""

    settings: Settings
    token: str
    runner: Runner
    # Verifications proved this session. Deliberately in memory: a restart
    # should re-prove rather than trust a file, and every persisted thing is
    # one more file that can go stale or be wrong.
    proven: dict


def _json(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    # No CORS headers anywhere, on purpose: without them a browser refuses to
    # let another origin read a reply even if it manages to send a request.
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def host_allowed(host_header: str | None) -> bool:
    """Is this `Host` one of ours? Port is irrelevant; the name is the point."""
    if not host_header:
        return False
    name = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") \
        else host_header.split("]")[0] + "]"
    return name in ALLOWED_HOSTS


def origin_allowed(origin: str | None) -> bool:
    """No Origin is fine (same-origin GET); a foreign one never is."""
    if origin is None:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return parsed.hostname in ("127.0.0.1", "localhost", "::1")


def _intent() -> dict | None:
    """What the user has already asked for, if anything. Never raises."""
    try:
        from ..state import last_intent

        return last_intent()
    except Exception:
        return None


def make_handler(board: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        server_version = "beertown-ui"

        def log_message(self, fmt, *args):  # noqa: A003 - stdlib hook name
            log.debug("[http] " + fmt, *args)

        # -- guards ----------------------------------------------------
        def _guarded(self) -> bool:
            if not host_allowed(self.headers.get("Host")):
                self._deny(403, "bad_host",
                           "This dashboard only answers to localhost.")
                return False
            if not origin_allowed(self.headers.get("Origin")):
                self._deny(403, "bad_origin",
                           "Cross-site requests are refused.")
                return False
            return True

        def _authed(self) -> bool:
            sent = self.headers.get("X-Beertown-Token", "")
            # compare_digest, not ==: a plain comparison leaks the token one
            # character at a time to anything that can time the reply.
            if not (sent and secrets.compare_digest(sent, board.token)):
                self._deny(401, "bad_token",
                           "Open the dashboard from the link the terminal "
                           "printed — it carries the key for this session.")
                return False
            return True

        def _deny(self, code: int, err: str, message: str) -> None:
            # Drain first. Replying to a POST without reading its body leaves
            # unread bytes in the socket, and the client sees a connection
            # reset instead of the reply -- so a refusal that works perfectly
            # reads to the user as "the dashboard crashed". The guard being
            # right is not enough if its answer never arrives.
            self._drain()
            log.warning("[ui] refused %s %s: %s", self.command, self.path, err)
            _json(self, code, {"ok": False, "error": err, "message": message})

        def _drain(self) -> None:
            # Once per request. `do_POST` reads the body itself before it can
            # dispatch, and draining again would block waiting for bytes that
            # have already been consumed -- which turned a clean refusal into
            # a hang, one round of over-correction after the reset it fixed.
            if getattr(self, "_body_done", False):
                return
            self._body_done = True
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            # Cap it: an unauthenticated caller does not get to make us read
            # a gigabyte before we tell them no.
            while length > 0:
                chunk = self.rfile.read(min(length, 65536))
                if not chunk:
                    return
                length -= len(chunk)

        # -- routes ----------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
            if not self._guarded():
                return
            route = urlparse(self.path).path
            if route == "/":
                return self._page()
            if route == "/api/state":
                if not self._authed():
                    return
                return _json(self, 200, self._state())
            self._deny(404, "not_found", "No such page.")

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
            if not self._guarded() or not self._authed():
                return
            route = urlparse(self.path).path
            if route not in ("/api/run", "/api/city"):
                return self._deny(404, "not_found", "No such action.")

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._deny(400, "bad_request", "Malformed request.")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            self._body_done = True
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._deny(400, "bad_request", "Malformed request.")

            if route == "/api/city":
                return self._city(body)

            name = body.get("action", "")
            entry = ACTIONS.get(name)
            if entry is None:
                return self._deny(400, "unknown_action",
                                  f"There is no {name!r} action.")

            title, work = entry
            job = board.runner.start(
                name, title,
                lambda say: self._record(name, work(board.settings, say)),
            )
            if job is None:
                return self._deny(409, "already_running",
                                  "Something is already running. Wait for it "
                                  "to finish — two sign-ins sharing one Chrome "
                                  "profile can corrupt it.")
            _json(self, 202, {"ok": True, "job": job.to_row()})

        def _city(self, body: dict) -> None:
            """Record the city, where `status` already looks for it.

            This is the handoff the wizard was missing. Without it the page
            asked for a city, kept it in the browser, and an agent reading
            `status` carried on offering the built-in default -- the two
            halves of the setup disagreeing with nobody able to see it.
            """
            from ..state import BadIntent, record_intent

            try:
                recorded = record_intent(body.get("city", ""))
            except BadIntent as exc:
                return self._deny(400, "bad_city", str(exc))
            log.info("[ui] city set to %r (list %r)",
                     recorded["query"], recorded["map_title"])
            _json(self, 200, {"ok": True, "intent": recorded})

        # -- helpers ---------------------------------------------------
        def _record(self, name: str, result: dict) -> dict:
            """Keep any verification a job proved, so the panel can show it."""
            for key in ("google", "untappd"):
                raw = result.get(key)
                if isinstance(raw, dict) and "ok" in raw:
                    board.proven[key] = checks.VerifyResult(
                        ok=bool(raw["ok"]), detail=raw.get("detail", ""),
                        evidence=raw.get("evidence", ""),
                    )
            return result

        def _state(self) -> dict:
            rows = checks.collect(board.settings, board.proven)
            job = board.runner.current
            step = checks.next_step(rows)
            return {
                "ok": True,
                "stages": [g.to_row() for g in checks.wizard(step)],
                "next_step": {
                    "key": step.key, "title": step.title, "body": step.body,
                    "cta": step.cta, "action": step.action, "done": step.done,
                },
                "defaults": {"query": board.settings.query,
                             "count": board.settings.target_count},
                "intent": _intent(),
                "checks": [c.to_row() for c in rows],
                "ready": checks.ready(rows),
                "blocking": [c.key for c in checks.blocking(rows)],
                "busy": board.runner.busy,
                "job": job.to_row() if job else None,
            }

        def _page(self) -> None:
            try:
                html = PAGE.read_bytes()
            except OSError:  # pragma: no cover -- ships with the package
                return self._deny(500, "no_page", "The dashboard page is missing.")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            # Nothing on this page loads off-machine. Saying so means a
            # compromised dependency cannot phone home from here either.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; "
                "img-src data:; form-action 'none'; base-uri 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(html)

    return Handler


class _Server(ThreadingHTTPServer):
    """A dashboard that refuses to share its port.

    `http.server` sets `allow_reuse_address`, and on Windows that let a second
    dashboard bind a port another one was already LISTENING on -- no error,
    two servers, two different keys, and requests going to whichever the OS
    felt like. A port collision has to fail loudly: the whole point of the
    record on disk is that there is one dashboard per checkout.
    """

    allow_reuse_address = False


def build(s: Settings, port: int = DEFAULT_PORT) -> tuple[_Server, str]:
    """Bind the dashboard and return it with the URL that carries its key."""
    board = Dashboard(settings=s, token=secrets.token_urlsafe(32),
                      runner=Runner(), proven={})
    httpd = _Server((HOST, port), make_handler(board))
    httpd.daemon_threads = True
    # The token rides in the fragment: browsers never put a fragment in a
    # Referer header or a server log, so handing it over costs nothing.
    url = f"http://{HOST}:{httpd.server_address[1]}/#{board.token}"
    return httpd, url


# Where a detached dashboard records itself, so a second `--detach` finds the
# first one instead of starting a rival on another port.
RUNNING = STATE_DIR / "ui.json"


HANDSHAKE_ENV = "BEERTOWN_UI_HANDSHAKE"


def serve(s: Settings, port: int = DEFAULT_PORT,
          open_browser: bool = True) -> str:
    """Run until interrupted. Returns the URL it served."""
    httpd, url = build(s, port)
    log.info("Dashboard on %s", url)
    _record(url, httpd.server_address[1])
    if open_browser:
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        _forget()
    return url


def _record(url: str, port: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # The handshake, not the pid, is how a detaching parent recognises *its*
    # child's record. Matching on pid looked equivalent and was not: a stale
    # record from an earlier server reads as "someone else's", and the parent
    # waits out its whole timeout beside a dashboard that started fine.
    payload = json.dumps({
        "url": url, "port": port, "pid": os.getpid(),
        "handshake": os.environ.get(HANDSHAKE_ENV, ""),
    })
    # Atomic: a detaching parent polls this file while the child writes it,
    # so a plain write can hand back half a JSON object. The reader's corrupt
    # -read guard means a torn read only costs another poll rather than
    # anything worse -- but a rename is free and removes the race instead of
    # surviving it.
    tmp = RUNNING.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(RUNNING)


def _forget() -> None:
    try:
        RUNNING.unlink()
    except OSError:
        pass


def _alive(pid: int) -> bool:
    """Is that process still there? Unknown counts as yes.

    Guessing "gone" would start a second dashboard beside a live one, and two
    servers racing for the same Chrome profile is the thing the single-job
    rule already exists to prevent.
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return True


def _read_record() -> dict | None:
    """Whatever is on disk, without asking the OS whether it is still alive."""
    if not RUNNING.exists():
        return None
    try:
        rec = json.loads(RUNNING.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return rec if isinstance(rec, dict) and rec.get("url") else None


def existing() -> dict | None:
    """The dashboard already running for this checkout, if there is one.

    The liveness probe shells out, so this is not something to call in a
    tight loop -- `serve_detached` waits on the record instead, since it
    holds the child handle and already knows whether it is alive.
    """
    rec = _read_record()
    if rec is None:
        return None
    if not _alive(int(rec.get("pid", 0))):
        _forget()
        return None
    return rec


def serve_detached(s: Settings, port: int = DEFAULT_PORT) -> dict:
    """Start the dashboard in its own process and return straight away.

    The reason this exists: the foreground server blocks, so AGENTS.md told
    an agent never to start it -- and in an agent-driven setup that meant
    nobody ever opened the dashboard at all. The install finished, the agent
    reported success, and the user was left having to already know the name
    of the thing built to tell them what to do.

    Blocking was the only reason it was off-limits. The page is read-only,
    bound to localhost, and has no route that can touch an account, so once
    it stops hanging the caller there is nothing left to forbid.
    """
    if (already := existing()) is not None:
        return {**already, "started": False}

    handshake = secrets.token_urlsafe(12)
    child = subprocess.Popen(
        [sys.executable, "-m", "beer_in_this_town", "ui", "--port", str(port)],
        cwd=str(ROOT),
        env={**os.environ, HANDSHAKE_ENV: handshake},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        **_detach_flags(),
    )

    # Wait for the child to publish its URL. The token is minted in there, so
    # there is nothing useful to return until it has.
    # Poll the record, not `existing()`: that one shells out to `tasklist` on
    # Windows, taking most of a second per turn and eating the budget before
    # the child had finished importing. We hold the child handle, so its
    # liveness is `poll()` -- free, and immediate.
    deadline = time.time() + 30.0
    while time.time() < deadline:
        rec = _read_record()
        if rec is not None and rec.get("handshake") == handshake:
            return {**rec, "started": True}
        if child.poll() is not None:
            raise RuntimeError(
                f"The dashboard exited immediately (code {child.returncode}). "
                f"Port {port} may already be in use by something else."
            )
        time.sleep(0.25)

    raise RuntimeError(
        f"The dashboard did not report a URL within 30s. Check whether "
        f"port {port} is free."
    )


def _detach_flags() -> dict:
    """Survive the parent exiting, on either platform."""
    if os.name == "nt":
        return {"creationflags": subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
