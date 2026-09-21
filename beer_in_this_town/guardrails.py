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

This module covers the **write** side only -- what protects the Google account
while `pin` and `notes` are running. The **read** side, which is what keeps the
Untappd scraping from being noticed, is a separate and less strict set: pacing,
the hourly ceiling, the disk cache, backoff, and robots.txt. Those live in
`http_client.py`, whose module docstring maps them the same way this one does.
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
BREAKER = STATE_DIR / "breaker.json"

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


def inspect_guardrails(
    limits: Limits, path: Path | None = None, breaker_path: Path | None = None
) -> dict[str, object]:
    """What the write guardrails currently say, without changing any of it.

    Several error remedies tell the caller to check `status` -- for a cool-off,
    for whether another run holds the budget -- and `status` could not see
    either. An agent that tripped a guardrail therefore asked, was told all
    clear, and re-ran straight back into it. AGENTS.md documented that hole
    rather than closing it.

    Read-only on purpose: `RateLedger` prunes in memory but this never calls
    anything that persists, so `status` stays the free, side-effect-free
    command it is advertised as.
    """
    path = path if path is not None else LEDGER
    ledger = RateLedger(limits, path=path)
    cooloff_h = ledger.cooloff_remaining_s() / 3600
    used = ledger.used_today()

    lock_path = path.with_suffix(".lock")
    lock: dict[str, object] | None = None
    if lock_path.exists():
        age = _lock_age_s(lock_path)
        try:
            body = json.loads(lock_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A half-written or clobbered lock still blocks a run, so saying
            # nothing about it is the one unhelpful answer.
            body = {}
        lock = {
            "pid": body.get("pid"),
            "age_h": round((age or 0) / 3600, 2),
            "stale": age is not None and age >= STALE_LOCK_SECONDS,
            "path": str(lock_path),
        }

    breaker = CircuitBreaker(limits.max_consecutive_failures, path=breaker_path)

    return {
        "used_today": used,
        "remaining_today": ledger.remaining_today(),
        "max_per_day": limits.max_per_day,
        "cooloff_remaining_h": round(cooloff_h, 2),
        "breaker": {"consecutive": breaker.consecutive,
                    "limit": breaker.limit,
                    "tripped": breaker.is_tripped},
        "can_write": (cooloff_h <= 0 and ledger.remaining_today() > 0
                      and not breaker.is_tripped),
        "ledger_corrupt": ledger.corrupt,
        "lock": lock,
    }


@contextmanager
def ledger_lock(path: Path | None = None) -> Iterator[None]:
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

    path = path if path is not None else LEDGER
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
    """Why the run stopped, and what to do -- which is never "delete this".

    The message used to end by inviting a manual delete, which AGENTS.md
    explicitly forbids and which throws away the only evidence that a run died
    mid-write. It is also unnecessary: an abandoned lock is broken
    automatically once it has been untouched for STALE_LOCK_SECONDS, because a
    live run refreshes it on every write.
    """
    hours = STALE_LOCK_SECONDS / 3600
    return (
        f"Another run is already running (lock at {lock}). Two runs sharing "
        "one budget would spend it twice over. Wait for it to finish -- there "
        "is no cool-off to sit out, and re-running now will only trip this "
        "again. "
        f"If nothing is actually running, a previous run was killed before it "
        f"could release the lock. Leave it alone: it is broken automatically "
        f"once untouched for {hours:.0f}h, and "
        "`python -m beer_in_this_town status --json` reports its age under "
        "write_guardrails.lock in the meantime."
    )


def single_writer(fn: Callable[P, T]) -> Callable[P, T]:
    """Mark a command as the only thing allowed to spend the budget while it runs.

    Applied to the two account-writing entry points. Held for the whole call,
    so `pin` and `notes` run one after another rather than overlapping.
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

    def __init__(self, limits: Limits, path: Path | None = None) -> None:
        self.limits = limits
    # Resolved at call time, not bound as a default: a default is
    # evaluated once at import, so anything that redirects the module
    # constant afterwards (tests, a relocated state dir) never reaches
    # it and the write lands in the real tree.
        self.path = path if path is not None else LEDGER
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
    """Stop after N consecutive failures instead of hammering a broken UI.

    The count is persisted. Without that, three failures on the last three
    places of a run tripped nothing -- `is_tripped` is only read at the top of
    the next iteration, and there was no next iteration -- and the following
    run started from zero. A UI that has stopped responding the way we expect
    is exactly when a script looks least human, and it does not start looking
    human again because the process restarted.
    """

    def __init__(self, limit: int, path: Path | None = None,
                 max_age_s: float = DEFAULT_COOLOFF_HOURS * 3600) -> None:
        self.limit = limit
        self.path = path if path is not None else BREAKER
        self.max_age_s = max_age_s
        self.consecutive = self._read()

    def _read(self) -> int:
        """The count, unless it is older than the cool-off it would cause.

        A run of failures says something about conditions *now*. Left to
        accumulate forever, three failures from last week trip a healthy run
        before it touches a single place -- and since the trip fires ahead of
        any work, no success can ever occur to clear it. That is a permanent
        lockout of both writing commands, which is a far worse failure than
        the gap persistence was added to close.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            count = int(raw["consecutive"])
            at = float(raw.get("at", 0.0))
        except (OSError, ValueError, TypeError, KeyError):
            return 0
        if _now() - at >= self.max_age_s:
            log.info("Discarding a circuit-breaker count older than %.0fh.",
                     self.max_age_s / 3600)
            return 0
        return count

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"consecutive": self.consecutive, "at": _now()}),
            encoding="utf-8",
        )

    def reset(self) -> None:
        """Clear the count. Called when a cool-off starts.

        The cool-off is the punishment for tripping; carrying the count past
        it would punish the next run for the same failures, forever.
        """
        self.consecutive = 0
        self._write()

    def record_success(self) -> None:
        self.consecutive = 0
        self._write()

    def record_failure(self) -> None:
        self.consecutive += 1
        self._write()

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
