"""The machine-readable contract between this tool and a coding agent.

Every command can emit a single JSON envelope on stdout instead of prose, so an
agent never has to parse log lines or guess whether something worked.

Design rules, deliberately strict:

  * **One envelope per invocation.** Printed last, on stdout, nothing after it.
    Logs go to stderr so stdout stays a clean JSON channel.
  * **Every envelope has the same shape**, so an agent can branch on `ok` and
    `next_actions` without knowing which command ran.
  * **Failures are data, not stack traces.** A non-zero exit still prints a
    valid envelope with `error.remedy` telling the agent what to do about it.
  * **`next_actions` are literal, runnable commands.** Not hints. An agent
    should be able to execute one verbatim.

The envelope is the public API. Treat changes to it as breaking.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "2.0"


@dataclass(frozen=True)
class Problem:
    """A failure an agent might be able to fix by itself."""

    code: str          # stable, greppable, e.g. "not_signed_in"
    message: str       # human-readable
    remedy: str        # what to DO about it, as a command or concrete step
    fatal: bool = True


@dataclass(frozen=True)
class Envelope:
    """The single object every command prints when --json is passed."""

    command: str
    ok: bool
    schema_version: str = SCHEMA_VERSION
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    # Prose for a human: things to do by hand, and things an agent must be
    # told to do rather than decide to do. `next_actions` promises literal
    # runnable commands, so a "# do this yourself" line does not belong in it
    # -- an agent passing argv as a list gets "#" as an argument, and one that
    # shells out silently no-ops and loops on the same suggestion.
    hints: list[str] = field(default_factory=list)
    error: Problem | None = None

    def to_json(self) -> str:
        payload = asdict(self)
        if payload.get("error") is None:
            payload.pop("error")
        # ASCII-escaped: "חיפה" as ח... parses back to the same text,
        # and prints on any console. Unescaped, a Windows console's cp1252
        # stdout raised after the command had done its work.
        return json.dumps(payload, indent=2, ensure_ascii=True)


def emit(envelope: Envelope, as_json: bool) -> None:
    """Print the envelope, or a short human summary when --json is absent."""
    if as_json:
        print(envelope.to_json())
        return

    status = "OK" if envelope.ok else "FAILED"
    print(f"\n[{status}] {envelope.command}")
    for key, value in envelope.data.items():
        print(f"  {key}: {value}")
    for warning in envelope.warnings:
        print(f"  ! {warning}")
    for hint in envelope.hints:
        print(f"  - {hint}")
    if envelope.error:
        print(f"  error: {envelope.error.message}")
        print(f"  fix:   {envelope.error.remedy}")
    if envelope.next_actions:
        print("\nNext:")
        for action in envelope.next_actions:
            print(f"  {action}")


def fail(command: str, problem: Problem, **data: Any) -> Envelope:
    return Envelope(
        command=command,
        ok=False,
        data=data,
        error=problem,
        next_actions=_runnable(problem.remedy),
        hints=[] if _runnable(problem.remedy) else [problem.remedy],
    )


# Commands that must never be handed to an agent to run: two write to the
# user's Google account, and `bootstrap` opens a browser and blocks for up to
# fifteen minutes waiting for a person.
_HUMAN_ONLY = ("bootstrap", " pin ", " notes ")


def _runnable(remedy: str) -> list[str]:
    """A remedy, promoted to a next action only if it is safe to execute.

    `fail()` used to promote anything starting with "python", which quietly
    put `bootstrap`, `pin` and `notes` back into `next_actions` after they had
    been removed from `status` -- so the contract still steered an agent into
    the account write, just through a different door. Whatever is not
    promoted still reaches the caller as a hint.
    """
    if not remedy.startswith("python"):
        return []
    padded = f" {remedy} "
    if any(token in padded for token in _HUMAN_ONLY):
        return []
    return [remedy]


def log_to_stderr() -> None:
    """Keep stdout a pure JSON channel."""
    import logging

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    root.addHandler(handler)
