"""A deliberately slow, cached, single-connection HTTP client.

Design goals, in priority order:
  1. Never get the Untappd account banned.
  2. Never silently return stale or wrong bytes.
  3. Speed (a distant third).

What actually protects the account, and where
---------------------------------------------
These are spread over several call sites, so here is the whole set in one
place. Read this before changing any number: each one is load-bearing, and the
ones that look like mere slowness are the ones doing the most work.

*Volume* -- the only measure that matters if the others fail.
  * `min_delay_s` / `max_delay_s` (2.0-4.5s, `config.py`) applied in
    `_throttle` before **every** request, jittered rather than fixed. A fixed
    interval is itself a signature; nothing human arrives on a metronome.
  * `hourly_budget` (600/h) is a hard ceiling per rolling hour, enforced by
    raising `BudgetExceeded` rather than by sleeping. Sleeping through a
    ceiling turns a stop into an unattended overnight crawl.
  * One connection, no concurrency. `PoliteClient` holds a single
    `httpx.Client` and every caller shares it; there is no code path that
    fans out. Concurrency is the single clearest tell of a script.
  * `cache_ttl_s` (12h) means a re-run costs almost no requests. Most reruns
    happen while debugging a parser, which is exactly when a naive client
    would hammer the same pages repeatedly.

*Reading the room* -- stopping when the far end signals displeasure.
  * 429/503 -> honour `Retry-After` if present, else climb the
    `backoff_ladder_s` (60s, 180s, 600s).
  * `max_consecutive_429` (3) -> `RateLimitTripped`, a deliberate abort. Not a
    longer sleep: three throttles in a row means the pacing is wrong for
    current conditions, and continuing is how a throttle becomes a block.
  * 403 -> stop immediately and tell the human to open the site in a browser.
    A 403 is usually an IP or account block already in progress, and retrying
    into one is the worst available move.

*Looking like the browser we claim to be* -- consistency, not disguise.
  * A real, current Chrome UA (`DEFAULT_UA`) plus the `Sec-Fetch-*`,
    `Accept-Language` and `Upgrade-Insecure-Requests` headers a real Chrome
    sends. The point is not to hide: it is that a client claiming to be Chrome
    while omitting headers every Chrome sends is more conspicuous than one
    claiming nothing. Keep the UA in step with the Chrome actually installed;
    a UA from a version that no longer exists is a cheap tell.
  * `xhr=True` swaps in the header set a real in-page fetch would carry, so
    the pagination requests match how the site issues them itself.
  * Session cookies come from the same Chrome profile `bootstrap` logged in
    with, so the requests belong to a real session rather than a fresh
    anonymous one that browses like a crawler.

*Consent* -- `robots_disallows_scraping` parses `robots.txt` with
`urllib.robotparser` and the run refuses to start if our paths are disallowed
(`respect_robots`, on by default). This is the one measure that is about their
wishes rather than our safety.

None of this makes scraping permitted -- Untappd's terms prohibit automated
access, and low volume is a mitigation, not an exemption. What it does is keep
the load negligible and make an accidental hammering impossible. The write
side (`pin`/`notes` against Google) has its own, stricter set; see
`guardrails.py`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .config import CACHE_DIR, STATE_DIR, Settings

log = logging.getLogger(__name__)


def _retry_after_seconds(header: str | None, fallback: int) -> float:
    """Parse Retry-After in either permitted form; never raise."""
    if not header:
        return fallback
    try:
        return max(0.0, float(int(header)))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(header)
        return max(0.0, when.timestamp() - time.time()) or fallback
    except Exception:
        return fallback


# Consecutive URLs that failed at the transport before the run stops.
# Mirrors max_consecutive_429: a handful of dead URLs is a flaky page,
# a run of them is a network that is not there.
READ_BUDGET = STATE_DIR / "read_budget.json"


class ReadBudget:
    """The hourly request ceiling, persisted like the write ledger is.

    The README calls 600/hour a hard cap. It lived in memory on the client, so
    restarting the process handed back a full allowance -- which makes it the
    one protection an ordinary retry loop could reset, while the write ledger
    beside it is on disk precisely so that cannot happen. A ceiling you can
    clear by starting again is a speed bump.
    """

    def __init__(self, s: Settings, path: Path | None = None) -> None:
        self.s = s
    # Resolved at call time, not bound as a default: a default is
    # evaluated once at import, so anything that redirects the module
    # constant afterwards (tests, a relocated state dir) never reaches
    # it and the write lands in the real tree.
        self.path = path if path is not None else READ_BUDGET
        self.window_start, self.count = self._read()

    def _read(self) -> tuple[float, int]:
        if not self.path.exists():
            return time.time(), 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            start = float(raw["window_start"])
            count = int(raw["count"])
        except (OSError, ValueError, TypeError, KeyError):
            # Fail CLOSED, like the write ledger this is modelled on. Reading
            # a damaged budget as "nothing spent" hands back a full allowance
            # for the price of one truncated write -- and this file is
            # rewritten up to 600 times an hour, so truncation is not exotic.
            log.warning("%s is unreadable; assuming the hour's budget is spent.",
                        self.path.name)
            return time.time(), self.s.hourly_budget
        if time.time() - start >= 3600:
            return time.time(), 0
        return start, count

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a crash mid-write left a truncated file, which the reader
        # above now (correctly) treats as a spent hour. Better not to produce
        # one in the first place.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"window_start": self.window_start, "count": self.count}),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def _roll(self) -> None:
        if time.time() - self.window_start >= 3600:
            self.window_start, self.count = time.time(), 0

    def remaining(self) -> int:
        self._roll()
        return max(0, self.s.hourly_budget - self.count)

    def record(self) -> None:
        self._roll()
        self.count += 1
        self._write()


MAX_CONSECUTIVE_TRANSPORT_ERRORS = 3


class TransportUnavailable(RuntimeError):
    """The connection itself is gone, repeatedly. Stop rather than sleep.

    There is a deliberate trip for consecutive 429s but there was none for a
    transport that simply is not there. Each venue retried three times over
    60/180/600s and `fetch_venues` swallowed the result per venue, so a dead
    network turned a hundred-venue run into roughly twenty-three hours of
    sleeping before the corpus gate finally failed on an empty result.
    """


class RateLimitTripped(RuntimeError):
    """We were throttled repeatedly. Stop before this becomes a ban."""


class BudgetExceeded(RuntimeError):
    """The hourly request budget was exhausted."""


def _cookies_from_storage_state(path: Path, domain_filter: str) -> dict[str, str]:
    """Extract cookies for one domain from a Playwright storage_state.json."""
    if not path.exists():
        log.warning(
            "No storage_state at %s -- continuing anonymously. "
            "Public stats will still work; the YOU column will be empty. "
            "Run bootstrap to fix.", path,
        )
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    return {
        c["name"]: c["value"]
        for c in state.get("cookies", [])
        if domain_filter in c.get("domain", "")
    }


class PoliteClient:
    """Serial, jittered, disk-cached HTTP GET with escalating backoff."""

    def __init__(self, settings: Settings, domain_filter: str = "untappd.com") -> None:
        self.s = settings
        self._client = httpx.Client(
            http2=True,
            timeout=settings.request_timeout_s,
            follow_redirects=True,
            cookies=_cookies_from_storage_state(settings.storage_state, domain_filter),
            # Claim to be Chrome, then send what Chrome sends. A request with a
            # Chrome UA and none of Chrome's fetch metadata is more obviously
            # scripted than one that claims nothing at all -- the mismatch is
            # the signal, not the UA. These are the headers a real top-level
            # navigation carries; `xhr=True` in get() swaps in the in-page
            # fetch set instead.
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;"
                          "q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        self._last_request_at = 0.0
        # On disk, so restarting does not refill the ceiling.
        self._budget = ReadBudget(self.s)
        self._consecutive_429 = 0
        self._consecutive_transport_errors = 0

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _throttle(self) -> None:
        """Gate every request on both the hourly ceiling and the per-request gap.

        Called from inside the retry loop, so retries are paced too -- a burst
        of retries against a struggling server is exactly the pattern that
        turns a transient error into a block.
        """
        now = time.time()
        if self._budget.remaining() <= 0:
            # Raise rather than sleep until the window rolls. A ceiling that
            # you can wait out is not a ceiling: it would quietly convert an
            # oversized job into an all-night crawl with nobody watching.
            raise BudgetExceeded(
                f"Hourly budget of {self.s.hourly_budget} requests exhausted. "
                "Re-run later; the disk cache means little work is repeated."
            )
        # Jittered, not fixed: a constant interval is its own signature.
        # Measured from the last request rather than slept unconditionally, so
        # time already spent parsing counts towards the gap.
        wait = random.uniform(self.s.min_delay_s, self.s.max_delay_s)
        elapsed = now - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._budget.record()

    @staticmethod
    def _cache_path(url: str, params: dict | None) -> Path:
        key = url + ("?" + urlencode(sorted((params or {}).items())) if params else "")
        return CACHE_DIR / (hashlib.sha256(key.encode()).hexdigest() + ".html")

    def _read_cache(self, path: Path) -> str | None:
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.s.cache_ttl_s:
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def get(
        self,
        url: str,
        params: dict | None = None,
        *,
        use_cache: bool = True,
        xhr: bool = False,
    ) -> str:
        cache_path = self._cache_path(url, params)
        if use_cache:
            cached = self._read_cache(cache_path)
            if cached is not None:
                log.debug("cache hit  %s", url)
                return cached

        headers = {}
        if xhr:
            headers = {
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/html, */*; q=0.01",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Referer": url,
            }

        last_error: Exception | None = None
        for attempt in range(self.s.max_retries):
            self._throttle()
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("transport error (%s), attempt %d", exc, attempt + 1)
                # Not after the last attempt: sleeping there buys nothing and
                # delayed the trip by the full ladder on every dead URL.
                if attempt < self.s.max_retries - 1:
                    time.sleep(self.s.backoff_ladder_s[min(attempt, 2)])
                continue

            self._last_request_at = time.time()
            self._consecutive_transport_errors = 0

            if resp.status_code in (429, 503):
                self._consecutive_429 += 1
                if self._consecutive_429 >= self.s.max_consecutive_429:
                    raise RateLimitTripped(
                        f"{self._consecutive_429} consecutive throttle responses from "
                        f"{url}. Aborting deliberately -- back off for a few hours "
                        "and lower target_count before retrying."
                    )
                fallback = self.s.backoff_ladder_s[min(attempt, 2)]
                # Retry-After is legally either seconds or an HTTP-date;
                # int() on the date form used to kill the run outright.
                delay = _retry_after_seconds(
                    resp.headers.get("Retry-After"), fallback
                )
                log.warning("HTTP %s -- sleeping %ss", resp.status_code, delay)
                time.sleep(delay)
                continue

            if resp.status_code == 403:
                raise RateLimitTripped(
                    "HTTP 403 from Untappd. This usually means an IP or account "
                    "block, not a bug. Stop running the script and check the site "
                    "manually in a browser before retrying."
                )

            resp.raise_for_status()
            self._consecutive_429 = 0
            html = resp.text
            if use_cache:
                cache_path.write_text(html, encoding="utf-8")
            return html

        # Every attempt for this URL failed at the transport. Count it: one
        # unreachable host looks the same as a hundred, and only the run of
        # them tells you the network is gone rather than a page being flaky.
        self._consecutive_transport_errors += 1
        if self._consecutive_transport_errors >= MAX_CONSECUTIVE_TRANSPORT_ERRORS:
            raise TransportUnavailable(
                f"{self._consecutive_transport_errors} consecutive URLs failed "
                f"at the transport ({last_error}). The network or the host is "
                f"gone; continuing would sleep through the backoff ladder once "
                f"per remaining venue."
            ) from last_error

        raise RuntimeError(
            f"GET failed after {self.s.max_retries} attempts: {url}"
        ) from last_error

    def robots_disallows_scraping(self) -> bool:
        """Would a compliant crawler be refused the paths we use?

        Delegated to urllib.robotparser rather than hand-rolled: the previous
        version matched only paths starting with /v/ or /search, so a blanket
        `Disallow: /` -- the strictest rule there is -- sailed straight through.
        It also mishandled grouped User-agent lines.
        """
        from urllib.robotparser import RobotFileParser

        try:
            txt = self.get("https://untappd.com/robots.txt", use_cache=False)
        except (RateLimitTripped, TransportUnavailable):
            # A 403 here is a block already in progress. Swallowing it read as
            # "robots does not forbid this" and carried on requesting into the
            # block -- the one move this module's 403 rule says never to make.
            raise
        except RuntimeError as exc:
            # Every attempt failed at the transport. TransportUnavailable
            # cannot fire here -- it needs three prior failed URLs and this is
            # the first request of a run -- so this branch is what a dead
            # network actually looks like at this point. Treating it as "no
            # prohibition" let the run proceed on a connection that is gone.
            raise TransportUnavailable(
                f"Could not reach robots.txt ({exc}). Refusing to start: a run "
                f"that cannot read the rules should not assume there are none."
            ) from exc
        except Exception:  # absence of robots.txt is not a prohibition
            return False

        parser = RobotFileParser()
        parser.parse(txt.splitlines())
        agent = self.s.user_agent
        return not all(
            parser.can_fetch(agent, path)
            for path in ("https://untappd.com/v/x/1", "https://untappd.com/search")
        )
