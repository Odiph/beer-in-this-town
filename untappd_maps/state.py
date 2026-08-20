"""Where is this project up to, and what should happen next?

This module exists so an agent never has to infer progress from log output or
guess whether a step already ran. `inspect_state()` reads the artifacts on disk
and reports a single, honest picture; `next_actions()` turns that into literal
commands to run.

That inversion is the point of the agent-led design: the tool owns the truth
about its own state, and the agent owns the judgement about what to do with it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA_DIR, STATE_DIR, Settings

PINNED = STATE_DIR / "pinned.json"
PREVIOUS_RUN = STATE_DIR / "previous_run.json"


@dataclass(frozen=True)
class Stage:
    """One step of the pipeline and whether it has produced its artifact."""

    name: str
    done: bool
    detail: str


def _latest(pattern: str) -> Path | None:
    matches = sorted(DATA_DIR.glob(pattern))
    return matches[-1] if matches else None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def inspect_state(s: Settings) -> dict[str, Any]:
    """A complete, machine-readable picture of progress. No side effects."""
    logged_in = s.storage_state.exists()
    csv_path = _latest("venues_*.csv") or _latest("seed_*.csv")
    kml_path = _latest("venues_*.kml")

    previous = _read_json(PREVIOUS_RUN)
    pinned = _read_json(PINNED)
    pin_counts = {
        "ok": sum(1 for v in pinned.values() if v == "ok"),
        "failed": sum(1 for v in pinned.values() if v == "failed"),
        "not_found": sum(1 for v in pinned.values() if v == "not-found"),
    }

    stages = [
        Stage("bootstrap", logged_in,
              f"session file {'present' if logged_in else 'missing'}"),
        Stage("scrape", csv_path is not None,
              str(csv_path) if csv_path else "no CSV in data/"),
        Stage("export_kml", kml_path is not None,
              str(kml_path) if kml_path else "no KML in data/"),
        Stage("pin", pin_counts["ok"] > 0,
              f"{pin_counts['ok']} saved, {pin_counts['failed']} failed, "
              f"{pin_counts['not_found']} not found"),
    ]

    return {
        "logged_in": logged_in,
        "latest_csv": str(csv_path) if csv_path else None,
        "latest_kml": str(kml_path) if kml_path else None,
        "venues_in_baseline": len(previous),
        "pin_progress": pin_counts,
        "stages": [{"name": st.name, "done": st.done, "detail": st.detail}
                   for st in stages],
    }


def next_actions(state: dict[str, Any], s: Settings) -> list[str]:
    """Literal commands to run next. Ordered; the first one is the recommendation."""
    if not state["logged_in"]:
        return [
            "python -m untappd_maps bootstrap",
            "# then: python -m untappd_maps selfcheck --json",
        ]

    if not state["latest_csv"]:
        return [
            f"python -m untappd_maps run --query {s.query} "
            f"--count {s.target_count} --no-upload --json",
        ]

    actions: list[str] = []
    if not state["latest_kml"]:
        actions.append(
            f"python -m untappd_maps run --query {s.query} "
            f"--count {s.target_count} --no-upload --json"
        )

    pins = state["pin_progress"]
    if pins["ok"] == 0:
        actions.append(
            f'python -m untappd_maps pin --csv "{state["latest_csv"]}" '
            f'--list "{s.map_title}" --limit 3 --json   # trial run first'
        )
    elif pins["failed"]:
        actions.append(
            f'python -m untappd_maps pin --csv "{state["latest_csv"]}" '
            f'--list "{s.map_title}" --json   # retries only what failed'
        )

    if not actions:
        actions.append("# pipeline complete -- re-run `run` to refresh the data")
    return actions
