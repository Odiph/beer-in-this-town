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

SCHEMA_VERSION = "1.0"


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
    error: Problem | None = None

    def to_json(self) -> str:
        payload = asdict(self)
        if payload.get("error") is None:
            payload.pop("error")
        return json.dumps(payload, indent=2, ensure_ascii=False)


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
        next_actions=[problem.remedy] if problem.remedy.startswith("python") else [],
    )


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
