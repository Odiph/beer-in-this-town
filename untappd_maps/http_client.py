"""A deliberately slow, cached, single-connection HTTP client.

Design goals, in priority order:
  1. Never get the Untappd account banned.
  2. Never silently return stale or wrong bytes.
  3. Speed (a distant third).
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .config import CACHE_DIR, Settings

log = logging.getLogger(__name__)


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
        self._window_start = time.time()
        self._window_count = 0
        self._consecutive_429 = 0

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _throttle(self) -> None:
        now = time.time()
        if now - self._window_start >= 3600:
            self._window_start, self._window_count = now, 0
        if self._window_count >= self.s.hourly_budget:
            raise BudgetExceeded(
                f"Hourly budget of {self.s.hourly_budget} requests exhausted. "
                "Re-run later; the disk cache means little work is repeated."
            )
        wait = random.uniform(self.s.min_delay_s, self.s.max_delay_s)
        elapsed = now - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._window_count += 1

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
                time.sleep(self.s.backoff_ladder_s[min(attempt, 2)])
                continue

            self._last_request_at = time.time()

            if resp.status_code in (429, 503):
                self._consecutive_429 += 1
                if self._consecutive_429 >= self.s.max_consecutive_429:
                    raise RateLimitTripped(
                        f"{self._consecutive_429} consecutive throttle responses from "
                        f"{url}. Aborting deliberately -- back off for a few hours "
                        "and lower target_count before retrying."
                    )
                fallback = self.s.backoff_ladder_s[min(attempt, 2)]
                delay = int(resp.headers.get("Retry-After", 0)) or fallback
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

        raise RuntimeError(
            f"GET failed after {self.s.max_retries} attempts: {url}"
        ) from last_error

    def robots_disallows_scraping(self) -> bool:
        """Crude but honest: is /v/ or /search Disallow-ed for User-agent: *?"""
        try:
            txt = self.get("https://untappd.com/robots.txt", use_cache=False)
        except Exception:
            return False
        in_star_block = False
        for raw in txt.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                in_star_block = value == "*"
            elif (key == "disallow" and in_star_block and value
                    and value.startswith(("/v/", "/search"))):
                return True
        return False
