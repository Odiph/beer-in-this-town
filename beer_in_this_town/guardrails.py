"""Guardrails whose only job is to keep the user's account safe.

The `pin` command automates a UI that Google's terms do not permit automating.
That means the realistic failure mode is not a crash — it is a *ban*. These
guardrails are therefore written to be paranoid and to fail CLOSED: when in
doubt, stop.

Four independent protections, deliberately layered so that defeating one still
leaves the others:

1. **RateLedger** — a persistent, rolling 24h budget of writes. Survives
   restarts, so you cannot reset the limit by re-running the command. This is
   the one that stops "just re-run it again" from becoming 400 saves in a day.
   Guarded by a **cross-process lock** (`ledger_lock`), because surviving a
   restart is worth nothing if two processes can spend the same budget at the
   same time — which the nightly catch-up task and a hand-run `pin` could.
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

import functools
import json
import logging
import os
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ParamSpec, TypeVar

from .config import STATE_DIR

log = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

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


# How long a lock may go untouched before it is treated as abandoned.
#
# This is a liveness threshold, not a run-duration budget: a live run refreshes
# its lock on every write (see `heartbeat`), so the age measured here is time
# since the holder last did anything, not time since it started. A run that is
# slow but working never looks stale, however long it takes.
STALE_LOCK_SECONDS = 2 * 3600


class Tripped(RuntimeError):
    """A guardrail fired. The caller must stop, not retry."""


class AlreadyRunning(Tripped):
    """Another run holds the ledger. Wait for it -- there is no cool-off to sit out."""


# The lock this process holds, as (path, token), or None. `heartbeat` needs to
# find it from inside RateLedger.record_write, which has no reference to the
# context manager that took it.
_held: tuple[Path, str] | None = None


@contextmanager
def ledger_lock(path: Path = LEDGER) -> Iterator[None]:
    """Hold exclusive ownership of the ledger for the length of a run.

    Without this, two processes -- the nightly catch-up task and someone
    running `pin` by hand, say -- each read the ledger at start-up, each saw
    the full remaining budget, and each spent it. Worse, `record_write`
    persists its own in-memory event list, so the second process to write
    discarded the first's events entirely: the budget was spent twice and the
    file showed only half of it. The ledger is the guardrail that surviving a
    restart is supposed to make undefeatable, so it has to be single-writer.

    `O_CREAT | O_EXCL` is the portable primitive -- one atomic create-or-fail
    on both Windows and POSIX, with no fcntl/msvcrt split. Every step that can
    hand ownership to somebody is built from an atomic operation, because the
    moment contention exists is the only moment any of this matters.

    Fails CLOSED throughout: uncertainty stops the run.
    """
    global _held

    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(8)

    _take(lock, token)
    _held = (lock, token)
    try:
        yield
    finally:
        _held = None
        _release(lock, token)


def heartbeat() -> None:
    """Mark the held lock as still alive. No-op when this process holds none.

    Without this, a lock's age means "time since the run started", so
    STALE_LOCK_SECONDS silently doubles as a cap on how long a run may take --
    and a slow-but-healthy run gets its lock stolen out from under it while it
    is still spending budget. Refreshing on every write makes age mean what
    the stale check assumes it means.
    """
    if _held is None:
        return
    lock, _token = _held
    try:
        os.utime(lock, None)
    except OSError:  # pragma: no cover -- lock already stolen or removed
        log.debug("Could not refresh the ledger lock at %s", lock)


def _take(lock: Path, token: str) -> None:
    """Acquire `lock`, breaking it only if it is genuinely abandoned."""
    try:
        _create_exclusive(lock, token)
        return
    except FileExistsError:
        pass

    age = _lock_age_s(lock)
    if age is None or age < STALE_LOCK_SECONDS:
        raise AlreadyRunning(_contended_message(lock)) from None

    log.warning(
        "Breaking a ledger lock at %s that has been untouched for %.1fh "
        "(limit %.1fh). A live run refreshes it on every write, so its owner "
        "is gone.", lock, age / 3600, STALE_LOCK_SECONDS / 3600,
    )
    _steal(lock)

    # Re-acquire exclusively rather than assuming the break was ours. Two
    # processes can reach this point together -- a scheduled run and a hand-run
    # starting the same minute is the whole reason this lock exists -- and a
    # plain O_CREAT|O_TRUNC here cannot fail, so both would proceed and spend
    # the budget twice. Exactly one wins the create; the other stops.
    try:
        _create_exclusive(lock, token)
    except FileExistsError:
        raise AlreadyRunning(_contended_message(lock)) from None


def _create_exclusive(lock: Path, token: str) -> None:
    """Create `lock` or raise FileExistsError. Never truncates an existing one."""
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        payload = {"pid": os.getpid(), "started": _now(), "token": token}
        os.write(fd, json.dumps(payload).encode("utf-8"))
    finally:
        # Closing in its own finally: on Windows an open handle would stop the
        # lock being removed later, stranding it until it goes stale.
        os.close(fd)


def _steal(lock: Path) -> None:
    """Take an abandoned lock out of the way, atomically.

    `os.replace` is the atomic step: of any number of processes that decide the
    same lock is stale, exactly one rename succeeds. The losers find it gone
    and fall through to the exclusive create, where they lose again -- and
    stop. Renaming rather than unlinking also leaves the evidence on disk if
    the break turns out to have been wrong.
    """
    stolen = lock.with_name(f"{lock.name}.stale.{os.getpid()}")
    try:
        os.replace(lock, stolen)
    except OSError:
        return  # someone else broke it first, or it was released meanwhile
    try:
        stolen.unlink()
    except OSError:  # pragma: no cover
        log.debug("Left a broken lock behind at %s", stolen)


def _release(lock: Path, token: str) -> None:
    """Remove the lock, but only if it is still the one we took.

    A blind unlink here is how mutual exclusion quietly stops existing: if this
    run's lock was broken as stale and another run took a fresh one, deleting
    it on the way out leaves the next process free to start alongside that run.
    Leaving a lock we do not own is the fail-closed choice -- worst case it
    goes stale and gets broken.
    """
    holder = _read_token(lock)
    if holder is None:
        log.warning(
            "The ledger lock at %s was gone before this run released it. "
            "Another run may have judged it stale and taken over.", lock,
        )
        return
    if holder != token:
        log.error(
            "Not removing the ledger lock at %s: it belongs to another run "
            "now. This run's lock was taken over while it was still working.",
            lock,
        )
        return
    try:
        lock.unlink()
    except OSError:  # pragma: no cover -- already gone, or unlinkable
        log.warning("Could not remove the ledger lock at %s", lock)


def _read_token(lock: Path) -> str | None:
    try:
        return str(json.loads(lock.read_text(encoding="utf-8"))["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _contended_message(lock: Path) -> str:
    return (
        f"Another run is already running (lock at {lock}). Two runs sharing "
        "one budget would spend it twice over. Wait for it to finish -- there "
        "is no cool-off to sit out, and re-running now will only trip this "
        "again. If you are certain nothing is running, a previous run was "
        f"killed: delete {lock.name}."
    )


def single_writer(fn: Callable[P, T]) -> Callable[P, T]:
    """Mark a command as the only thing allowed to spend the budget while it runs.

    Applied to the two account-writing entry points. Held for the whole call,
    so `pin` and `notes` run one after another -- which is what
    run_catchup.ps1 already does -- rather than overlapping.
    """
    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        with ledger_lock():
            return fn(*args, **kwargs)

    return wrapper


def _lock_age_s(lock: Path) -> float | None:
    """Seconds since the lock was taken, or None if that cannot be told."""
    try:
        return max(0.0, _now() - lock.stat().st_mtime)
    except OSError:
        return None


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
        heartbeat()

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
