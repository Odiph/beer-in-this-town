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

from .config import DATA_DIR, STATE_DIR, Settings, scope_slug

LAST_RUN = STATE_DIR / "last_run.json"
LEGACY_PINNED = STATE_DIR / "pinned.json"


def record_run(query: str, map_title: str, csv_path: Path) -> None:
    """Remember what the last `run` actually did.

    `status` used to answer from `Settings()` defaults, because it is not one
    of the commands that derive settings from argv. So after `run --query
    london` it reported Singapore and handed an agent a literal `pin` command
    aiming a London CSV at a Singapore list -- a write to a live account, from
    the one command documented as read-only.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(
        json.dumps({"query": query, "map_title": map_title,
                    "csv": str(csv_path)}, indent=1),
        encoding="utf-8",
    )


@dataclass(frozen=True)
class Stage:
    """One step of the pipeline and whether it has produced its artifact."""

    name: str
    done: bool
    detail: str


def _latest(pattern: str) -> Path | None:
    """Newest by modification time.

    Sorting lexicographically put venues_singapore_2026-08-01.csv after
    venues_london_2026-08-21.csv, so `next_actions` would hand an agent -- whose
    contract says to run the first action verbatim -- a stale dataset.
    """
    matches = list(DATA_DIR.glob(pattern))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


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

    last_run = _read_json(LAST_RUN)
    query = last_run.get("query") or s.query
    list_name = last_run.get("map_title") or s.map_title

    previous = _read_json(STATE_DIR / f"previous_run_{scope_slug(query)}.json")
    # Scoped per list, so this counts progress on the list actually in play
    # rather than every place ever saved from this machine.
    pinned = _read_json(STATE_DIR / f"pinned_{scope_slug(list_name)}.json")
    pin_scope = list_name
    if not pinned and LEGACY_PINNED.exists():
        # Pre-upgrade progress. Report it rather than tell someone with fifty
        # saved places that they have none -- but only read it. Adopting it
        # belongs to `pin`, which knows which list it is talking about.
        pinned = _read_json(LEGACY_PINNED)
        pin_scope = "unscoped (pre-upgrade)"
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
        "last_run": {"query": query, "map_title": list_name,
                     "recorded": bool(last_run)},
        "latest_csv": str(csv_path) if csv_path else None,
        "latest_kml": str(kml_path) if kml_path else None,
        "venues_in_baseline": len(previous),
        "pin_progress": pin_counts,
        "pin_progress_scope": pin_scope,
        "stages": [{"name": st.name, "done": st.done, "detail": st.detail}
                   for st in stages],
    }


def next_actions(state: dict[str, Any], s: Settings) -> list[str]:
    """Literal commands to run next. Ordered; the first one is the recommendation."""
    if not state["logged_in"]:
        return [
            "python -m beer_in_this_town bootstrap",
            "# then: python -m beer_in_this_town selfcheck --json",
        ]

    query = state.get("last_run", {}).get("query") or s.query
    list_name = state.get("last_run", {}).get("map_title") or s.map_title

    if not state["latest_csv"]:
        return [
            f"python -m beer_in_this_town run --query {query} "
            f"--count {s.target_count} --no-upload --json",
        ]

    actions: list[str] = []
    if not state["latest_kml"]:
        actions.append(
            f"python -m beer_in_this_town run --query {query} "
            f"--count {s.target_count} --no-upload --json"
        )

    pins = state["pin_progress"]
    if pins["ok"] == 0:
        actions.append(
            f'python -m beer_in_this_town pin --csv "{state["latest_csv"]}" '
            f'--list "{list_name}" --limit 3 --json   # trial run first'
        )
    elif pins["failed"]:
        actions.append(
            f'python -m beer_in_this_town pin --csv "{state["latest_csv"]}" '
            f'--list "{list_name}" --json   # retries only what failed'
        )

    if not actions:
        actions.append("# pipeline complete -- re-run `run` to refresh the data")
    return actions
