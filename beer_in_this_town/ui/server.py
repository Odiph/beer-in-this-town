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
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ..config import Settings
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
            if route != "/api/run":
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
                "next_step": {
                    "key": step.key, "title": step.title, "body": step.body,
                    "cta": step.cta, "action": step.action, "done": step.done,
                },
                "defaults": {"query": board.settings.query,
                             "count": board.settings.target_count},
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


def build(s: Settings, port: int = DEFAULT_PORT) -> tuple[ThreadingHTTPServer, str]:
    """Bind the dashboard and return it with the URL that carries its key."""
    board = Dashboard(settings=s, token=secrets.token_urlsafe(32),
                      runner=Runner(), proven={})
    httpd = ThreadingHTTPServer((HOST, port), make_handler(board))
    httpd.daemon_threads = True
    # The token rides in the fragment: browsers never put a fragment in a
    # Referer header or a server log, so handing it over costs nothing.
    url = f"http://{HOST}:{httpd.server_address[1]}/#{board.token}"
    return httpd, url


def serve(s: Settings, port: int = DEFAULT_PORT,
          open_browser: bool = True) -> str:
    """Run until interrupted. Returns the URL it served."""
    httpd, url = build(s, port)
    log.info("Dashboard on %s", url)
    if open_browser:
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return url
