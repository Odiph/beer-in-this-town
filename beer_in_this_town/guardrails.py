"""Guardrails whose only job is to keep the user's account alive.

The `pin` command automates a UI that Google's terms do not permit automating.
That means the realistic failure mode is not a crash — it is a *ban*. These
guardrails are therefore written to be paranoid and to fail CLOSED: when in
doubt, stop.

Four independent protections, deliberately layered so that defeating one still
leaves the others:

1. **RateLedger** — a persistent, rolling 24h budget of writes. Survives
   restarts, so you cannot reset the limit by re-running the command. This is
   the one that stops "just re-run it again" from becoming 400 saves in a day.
2. **CircuitBreaker** — consecutive failures trip it. If the UI stops
   responding the way we expect, that is exactly when a scripted client looks
   least human, so we stop rather than hammer.
3. **Block detection** — scans the live page for interstitials (CAPTCHA,
   "unusual traffic", forced sign-out). Any hit aborts the run immediately and
   records a cool-off.
4. **Cool-off** — after a trip or a detected block, refuse to run again for a
   fixed period, persisted to disk.

None of this makes automating the Maps UI permitted. It reduces the chance of
tripping Google's abuse heuristics, which is a different and lesser claim.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .config import STATE_DIR

log = logging.getLogger(__name__)

LEDGER = STATE_DIR / "rate_ledger.json"

# Conservative defaults. A human adding bars to a list does not do 200 in a day.
DEFAULT_MAX_PER_RUN = 60
DEFAULT_MAX_PER_DAY = 100
DEFAULT_COOLOFF_HOURS = 6.0
DEFAULT_BREAK_EVERY = 15      # places
DEFAULT_BREAK_SECONDS = 90.0  # a plausible "put the phone down" pause

# Substrings that mean Google has noticed us. Lowercased before comparison.
BLOCK_SIGNALS = (
    "unusual traffic",
    "our systems have detected",
    "captcha",
    "recaptcha",
    "verify it's you",
    "verify it is you",
    "confirm you're not a robot",
    "not a robot",          # "...sending the requests, not a robot" -- the real wording
    "unusual activity",
    "automated queries",
    "suspicious activity",
    "sorry/index",
    "temporarily blocked",
    "too many requests",
)


class Tripped(RuntimeError):
    """A guardrail fired. The caller must stop, not retry."""


@dataclass(frozen=True)
class Limits:
    """Immutable knobs. Lowering these is safe; raising them is the user's call."""

    max_per_run: int = DEFAULT_MAX_PER_RUN
    max_per_day: int = DEFAULT_MAX_PER_DAY
    cooloff_hours: float = DEFAULT_COOLOFF_HOURS
    break_every: int = DEFAULT_BREAK_EVERY
    break_seconds: float = DEFAULT_BREAK_SECONDS
    max_consecutive_failures: int = 3


def _now() -> float:
    return time.time()


class RateLedger:
    """A rolling 24h write budget that persists across runs.

    Deliberately stored on disk: the whole point is that restarting the process
    does not hand you a fresh allowance.
    """

    def __init__(self, limits: Limits, path: Path = LEDGER) -> None:
        self.limits = limits
        self.path = path
        self.corrupt = False
        raw = self._read()
        self.events: list[float] = list(raw.get("events", []))
        self.blocked_until: float = float(raw.get("blocked_until", 0.0))

    # -- persistence -----------------------------------------------------
    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Fail CLOSED. A corrupt ledger previously meant "fresh 100-write
            # allowance and no cool-off" -- so a crash right after a CAPTCHA
            # (which is exactly when a truncated write is likely, since we
            # write after every save) handed back a clean slate. Assume the
            # worst instead: full day used, cool-off running.
            log.error(
                "Rate ledger at %s is unreadable. Assuming the budget is spent "
                "and a cool-off is active -- this is deliberate. Delete the file "
                "only if you are certain no run was interrupted.", self.path,
            )
            self.corrupt = True
            return {
                "events": [_now()] * self.limits.max_per_day,
                "blocked_until": _now() + self.limits.cooloff_hours * 3600,
            }

    def _write(self) -> None:
        """Atomic write: a crash mid-write must not truncate the ledger."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"events": self.events, "blocked_until": self.blocked_until}, indent=1
        )
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)  # atomic on POSIX and Windows

    # -- queries ---------------------------------------------------------
    def _prune(self) -> None:
        cutoff = _now() - 24 * 3600
        self.events = [t for t in self.events if t >= cutoff]

    def used_today(self) -> int:
        self._prune()
        return len(self.events)

    def remaining_today(self) -> int:
        return max(0, self.limits.max_per_day - self.used_today())

    def cooloff_remaining_s(self) -> float:
        return max(0.0, self.blocked_until - _now())

    # -- gates -----------------------------------------------------------
    def assert_can_start(self) -> None:
        """Fail closed before a run begins."""
        remaining_cooloff = self.cooloff_remaining_s()
        if remaining_cooloff > 0:
            raise Tripped(
                f"Cool-off active: {remaining_cooloff / 3600:.1f}h remaining. "
                "A previous run was blocked or tripped a guardrail. "
                "Waiting is the point -- do not delete state/rate_ledger.json."
            )
        if self.remaining_today() <= 0:
            raise Tripped(
                f"Daily write budget exhausted ({self.limits.max_per_day} saves "
                "in the last 24h). This limit exists to keep the account safe. "
                "Resume tomorrow; progress is journalled."
            )

    def budget_for_this_run(self, requested: int | None) -> int:
        """How many places this run may touch, given every ceiling."""
        allowed = min(self.limits.max_per_run, self.remaining_today())
        return allowed if requested is None else min(requested, allowed)

    # -- recording -------------------------------------------------------
    def record_write(self) -> None:
        """Charge one write to the budget.

        Call this BEFORE the interaction, not after. Every Save click is a
        real mutation request to Google whether or not our verification
        later agrees, and a retry is another one. Over-counting costs a few
        places a day; under-counting costs the account.
        """
        self.events.append(_now())
        self._write()

    def start_cooloff(self, reason: str) -> None:
        self.blocked_until = _now() + self.limits.cooloff_hours * 3600
        self._write()
        log.error(
            "Cool-off started (%.1fh): %s", self.limits.cooloff_hours, reason
        )


class CircuitBreaker:
    """Stop after N consecutive failures instead of hammering a broken UI."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.consecutive = 0

    def record_success(self) -> None:
        self.consecutive = 0

    def record_failure(self) -> None:
        self.consecutive += 1

    @property
    def is_tripped(self) -> bool:
        return self.consecutive >= self.limit


def detect_block(page_text: str) -> str | None:
    """Return the matched signal if the page looks like an interstitial.

    Checked against page text rather than status codes: Google serves these as
    HTTP 200 pages, so there is no error to catch.
    """
    haystack = page_text.lower()
    for signal in BLOCK_SIGNALS:
        if signal in haystack:
            return signal
    return None


def looks_signed_out(page_text: str) -> bool:
    """A mid-run sign-out usually means the session was invalidated."""
    lowered = page_text.lower()
    return "sign in" in lowered and "saved" not in lowered
