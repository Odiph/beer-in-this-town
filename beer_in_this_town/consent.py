"""Recorded consent for the commands that write to a Google account (#22).

`pin` and `notes` automate the Google Maps UI, which Google's terms do not
permit, and they write into the user's own account. AGENTS.md rule 2 said
"never run `pin` without an explicit human instruction" -- and nothing
checked it. A rule enforced by an agent choosing to obey a markdown file is
a request, not a guardrail.

This is the check. A person runs `allow-writes --list "<name>"` at their own
terminal, reads what the two commands do and what they cross, and types the
list's name back. That records consent for that one list, for a bounded
number of days, in `state/consent.json`. `pin` and `notes` refuse with
`no_consent` before the pre-flight or any browser launch when there is none.

Three properties matter more than the rest:

* **Fail closed.** A missing, corrupt, unreadable or implausible record is
  no consent. Nothing here ever turns an error into a yes.
* **Per list, exactly.** Keyed by `scope_slug` like every other per-list
  journal, and then checked against the exact name the person typed, so
  consent for "London Bars" covers neither "London Bars Test" nor
  "london bars".
* **Bounded.** At most `MAX_DAYS`. Consent given for a weekend's pinning
  should not still be standing next season.

It is not tamper-proof: anything that can write `state/` can write this
file, just as it could delete the rate ledger. What it removes is the
ordinary path by which an agent following the loop, or following a hint,
drifts into a write nobody asked for.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from . import config

CONSENT_FILE = "consent.json"
DEFAULT_DAYS = 7
MAX_DAYS = 30
_DAY_S = 24 * 3600
# Clock skew tolerated on `granted_at`, so a record written a moment ago by a
# process whose clock is slightly ahead is not thrown away.
_SKEW_S = 300


def consent_path() -> Path:
    """Where the record lives. Resolved per call, so a redirected state dir
    (tests, a relocated install) is honoured rather than frozen at import."""
    return config.STATE_DIR / CONSENT_FILE


def _read_all() -> dict[str, Any]:
    """Every stored entry, unvalidated. Any failure reads as nothing."""
    path = consent_path()
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _valid(key: str, entry: Any, now: float) -> dict[str, Any] | None:
    """The entry if it is a live, well-formed grant filed under its own key."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("list")
    granted = _number(entry.get("granted_at"))
    expires = _number(entry.get("expires_at"))
    if not isinstance(name, str) or granted is None or expires is None:
        return None
    if config.scope_slug(name) != key:
        return None
    if granted > now + _SKEW_S or expires <= now:
        return None
    if expires - granted > MAX_DAYS * _DAY_S:
        return None
    return {"list": name, "granted_at": granted, "expires_at": expires}


def active_consents(now: float | None = None) -> list[dict[str, Any]]:
    """Every live grant, soonest to expire first. Never raises."""
    now = time.time() if now is None else now
    live = (_valid(k, v, now) for k, v in _read_all().items())
    return sorted((e for e in live if e), key=lambda e: e["expires_at"])


def has_consent(list_name: str, now: float | None = None) -> bool:
    """Is there live consent for writes to exactly this list?"""
    if not isinstance(list_name, str) or not list_name.strip():
        return False
    now = time.time() if now is None else now
    key = config.scope_slug(list_name)
    entry = _valid(key, _read_all().get(key), now)
    return entry is not None and entry["list"] == list_name


def _write(entries: dict[str, Any]) -> None:
    path = consent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    tmp.replace(path)


def grant(list_name: str, days: int = DEFAULT_DAYS,
          now: float | None = None) -> dict[str, Any]:
    """Record consent for one list. Only `allow-writes` should call this.

    Expired and malformed entries are dropped on the way through, so the file
    only ever holds what is live.
    """
    if not isinstance(list_name, str) or not list_name.strip():
        raise ValueError("A list name is needed.")
    if isinstance(days, bool) or not isinstance(days, int) \
            or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"--days must be between 1 and {MAX_DAYS}.")
    now = time.time() if now is None else now
    record = {"list": list_name, "granted_at": now,
              "expires_at": now + days * _DAY_S}
    key = config.scope_slug(list_name)
    kept = {k: v for k, v in _read_all().items() if _valid(k, v, now)}
    _write({**kept, key: record})
    return record


def revoke(list_name: str) -> bool:
    """Remove consent for one list. True if there was an entry to remove."""
    key = config.scope_slug(list_name)
    entries = _read_all()
    if key not in entries:
        return False
    _write({k: v for k, v in entries.items() if k != key})
    return True


def report(now: float | None = None) -> list[dict[str, Any]]:
    """What `status` shows: each live grant and how long it has left."""
    now = time.time() if now is None else now
    return [{**e, "remaining_h": round((e["expires_at"] - now) / 3600, 1)}
            for e in active_consents(now)]
