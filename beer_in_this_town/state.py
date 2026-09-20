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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA_DIR, STATE_DIR, Settings, scope_slug
from .guardrails import Limits, inspect_guardrails

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
    # A file, not a working account. `verify` is what settles that, and the
    # name is kept only because it is part of the published envelope.
    logged_in = s.storage_state.exists()
    verification = last_verification()
    csv_path = _latest("venues_*.csv") or _latest("seed_*.csv")
    kml_path = _latest("venues_*.kml")

    last_run = _read_json(LAST_RUN)
    # A real run outranks an intention, which outranks the built-in default.
    # The middle one is what the dashboard writes, and is the whole reason
    # the city a person types reaches the agent at all.
    intent = last_intent() or {}
    query = last_run.get("query") or intent.get("query") or s.query or ""
    list_name = (last_run.get("map_title") or intent.get("map_title")
                 or s.map_title or "")

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
        # What `verify` last proved, or None when nothing has been
        # proved recently. `logged_in` is a file; this is evidence.
        "verification": verification,
        # What the user asked for, and where the query above came from. An
        # agent that sees `intent` knows the city is a choice rather than a
        # default it should ask about.
        "intent": intent or None,
        "last_run": {"query": query, "map_title": list_name,
                     "recorded": bool(last_run)},
        "latest_csv": str(csv_path) if csv_path else None,
        "latest_kml": str(kml_path) if kml_path else None,
        "venues_in_baseline": len(previous),
        "pin_progress": pin_counts,
        "pin_progress_scope": pin_scope,
        # The write guardrails, which `status` could not previously see even
        # though several error remedies send the caller here to check them.
        "write_guardrails": inspect_guardrails(Limits()),
        "stages": [{"name": st.name, "done": st.done, "detail": st.detail}
                   for st in stages],
    }


# What the user said they wanted, before any run has happened. Deliberately
# NOT `last_run.json`: `status` reports `recorded: bool(last_run)`, so writing
# an intention there would claim a run had happened that had not.
INTENT = STATE_DIR / "intent.json"

# A city reaches the filesystem through `scope_slug`, which sanitises it, and
# reaches Untappd as a search term. Neither needs it to be long.
MAX_QUERY_LEN = 80


class BadIntent(ValueError):
    """The city was empty, or not something worth writing down."""


def record_intent(query: str, map_title: str | None = None) -> dict:
    """Remember the city the user asked for, before there is a run to record.

    The dashboard asks for a city and the agent reads `status`; without this
    the two never met. The wizard would hand a person a London command while
    `next_actions` offered Singapore, and nothing anywhere knew they
    disagreed.

    The map title is derived rather than left alone, because leaving it is the
    bug `record_run`'s docstring describes: a London CSV aimed at a Singapore
    list, which is a write to a live account.
    """
    cleaned = " ".join((query or "").split())
    if not cleaned:
        raise BadIntent("A city is needed.")
    if len(cleaned) > MAX_QUERY_LEN:
        raise BadIntent(f"That city name is longer than {MAX_QUERY_LEN} "
                        f"characters.")

    title = map_title or f"{cleaned.title()} Bars"
    payload = {"query": cleaned, "map_title": title, "at": time.time()}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INTENT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(INTENT)
    return payload


def last_intent() -> dict | None:
    """What the user last asked for, if anything. Never raises."""
    if not INTENT.exists():
        return None
    try:
        rec = json.loads(INTENT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return rec if isinstance(rec, dict) and rec.get("query") else None


# Where `verify` records what it proved. Deliberately short-lived: a session
# that worked this morning can be dead by lunchtime, and a file with no age on
# it is exactly the "a cookie means a working account" mistake in a new place.
VERIFICATION = STATE_DIR / "verification.json"
VERIFICATION_TTL_S = 12 * 3600


def record_verification(accounts: dict, ok: bool) -> None:
    """Persist what a `verify` run established, so the loop can terminate.

    The dashboard keeps its results in memory on purpose -- a restart should
    re-prove rather than trust a file. The agent loop cannot: without a record
    it re-verifies on every pass and never gets to `run`. The TTL is what
    keeps the two positions honest with each other.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"at": time.time(), "ok": ok, "accounts": accounts})
    tmp = VERIFICATION.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(VERIFICATION)


def last_verification() -> dict | None:
    """The most recent `verify`, if it is recent enough to still mean anything."""
    if not VERIFICATION.exists():
        return None
    try:
        rec = json.loads(VERIFICATION.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(rec, dict) or "at" not in rec:
        return None
    age = time.time() - float(rec["at"])
    if age > VERIFICATION_TTL_S:
        return None
    return {**rec, "age_h": round(age / 3600, 1)}


def _serving() -> dict | None:
    """The dashboard running for this checkout, if any. Never raises."""
    try:
        from .ui import existing

        return existing()
    except Exception:  # a UI that cannot even be imported is not running
        return None


def blocked_on(state: dict[str, Any]) -> str | None:
    """What a person -- not an agent -- has to do before the loop can go on.

    `next_actions` empties for two completely different reasons: the work is
    finished, or it cannot proceed without a human. AGENTS.md says an empty
    list is the end of the loop and not an error, which is true in both
    cases, and useless to an agent that has to report why it stopped. This
    names it.

    Note what does NOT count as blocked: an untested session. That is work an
    agent can do -- `verify` needs no password -- so it belongs in
    `next_actions`, not here.
    """
    if not state["logged_in"]:
        return "sign_in"
    verification = state.get("verification")
    if verification and not verification.get("ok"):
        # Tested, and one of the accounts is signed out. Only a person can
        # fix that, whatever the file on disk says.
        return "sign_in"
    if not state["last_run"].get("query"):
        # Nobody has said where. There is no default to fall back on and
        # there should not be: picking a city for someone is picking what
        # they get, and doing it silently is worse than asking.
        return "choose_city"
    return None


def next_actions(state: dict[str, Any], s: Settings) -> list[str]:
    """Literal commands to run next. Ordered; the first one is the recommendation.

    Only read-only, safe commands appear here. `pin` and `notes` write to the
    user's Google account and AGENTS.md says an agent must never run them
    without being asked -- while the loop this list defines says to run the
    first action and repeat until it is empty. Listing them made those two
    rules contradict each other, and the loop won: `pin` was the only action
    that ever terminated it. What a human might want to do next lives in
    `hints`, which nothing is instructed to execute.
    """
    query = state.get("last_run", {}).get("query") or s.query

    # This used to hand a brand-new user `run`, on the reasoning that a
    # session is only needed for the YOU column and that `run --no-upload`
    # "would have worked fine" without one. It does not. Signed out of
    # Untappd, search stops at 5 results, so that run spends its requests
    # building a five-venue corpus that looks like a completed scrape --
    # and the diff, the baseline and the KML are then all wrong together.
    #
    # Handing back `selfcheck` here was worse: nothing selfcheck does changes
    # `logged_in`, so an agent following the loop in AGENTS.md -- run the
    # first action, re-read status, repeat -- ran it forever. The previous
    # `run` at least terminated, badly. A non-terminating loop is the worse
    # of the two.
    #
    # AGENTS.md already defines the right answer: an empty list is the end of
    # the loop, not an error. Being blocked on a human IS the end of the
    # loop, and `data.blocked_on` says so in a field an agent can branch on.
    # `verify` is what an agent runs afterwards to find out whether the human
    # actually finished.
    if not state["logged_in"]:
        # Opening the dashboard IS the next action, and an agent can now do
        # it: `--detach` returns instead of blocking, and the page is
        # read-only with no route that can touch an account. Leaving it out
        # is what stranded the user -- the agent finished the install, said
        # "ready", and the one thing that would have told them what to do
        # next was the one thing it had been told not to run.
        #
        # Offered only while nothing is serving. Once it is up, the loop ends:
        # re-launching a running dashboard forever is the same
        # non-termination as the `selfcheck` it replaced.
        from .ui import existing

        if existing() is None:
            return ["python -m beer_in_this_town ui --detach --json"]
        return []

    # `logged_in` means the file exists, which is not the same as the accounts
    # working -- on a machine whose sessions had both expired, this list
    # handed an agent `run`, and signed out of Untappd that builds a
    # five-venue corpus and reports a finished scrape. The dashboard has
    # drawn this distinction since it was written; the agent contract had
    # not. `verify` is cheap, offline of any account write, and terminates.
    verification = state.get("verification")
    if verification is None:
        return ["python -m beer_in_this_town verify --json"]
    if not verification.get("ok"):
        return []          # signed out: blocked_on says a person is needed

    if not query:
        # Nothing to offer: `run` without a city has nowhere to go, and the
        # dashboard is where a person names one.
        from .ui import existing

        if existing() is None:
            return ["python -m beer_in_this_town ui --detach --json"]
        return []

    if state["latest_csv"] and state["latest_kml"]:
        # Nothing further an agent should start on its own. An empty list is
        # what AGENTS.md defines as the end of the loop, and now that the
        # account-writing commands are out of it the loop can actually reach
        # that end -- previously `pin` was the only thing that terminated it.
        return []

    return [
        f'python -m beer_in_this_town run --query "{query}" '
        f"--count {s.target_count} --no-upload --json"
    ]


def hints(state: dict[str, Any], s: Settings) -> list[str]:
    """What a person might want to do next. Never executed by anything."""
    # Keyed off `blocked_on`, not off the session file. A tested-and-expired
    # session sets `blocked_on` to sign_in while `logged_in` stays true, and
    # this used to read only the file -- so the envelope named the right
    # problem in `error.code` and said nothing about it to the person who had
    # to fix it, talking about ledger locks instead.
    if blocked_on(state) == "choose_city":
        return [
            "No city chosen, and there is no default -- picking one for "
            "someone is picking what they get. Open `beertown ui` and name a "
            "city on the last step, or pass `run --query \"<city>\"`.",
        ] + ([f"The dashboard is already open at {running['url']}."]
             if (running := _serving()) else [])

    if blocked_on(state) == "sign_in":
        expired = bool(state.get("verification"))
        return [
            ("A saved session is present and was tested: one of the accounts "
             "is signed out. " if expired else "")
            + "Blocked on a person: this needs a password, so no agent can do "
            "it. Run `beertown ui` yourself -- it opens a "
            "dashboard that signs you in and then tests both accounts "
            "for real, rather than trusting a cookie means they work.",
            "Both accounts matter, not just Google. Signed out of Untappd, "
            "search stops at 5 results, so a run would build a five-venue "
            "corpus and report it as a finished scrape.",
            "Once that is done, `python -m beer_in_this_town verify --json` "
            "checks both accounts for real and needs no browser -- an agent "
            "can run it to find out whether the sign-in actually took.",
        ] + ([f"The dashboard is already open at {running['url']} — send them "
              f"there rather than starting another."]
             if (running := _serving()) else [])

    out: list[str] = []
    guards = state.get("write_guardrails") or {}
    if guards.get("cooloff_remaining_h"):
        out.append(
            f"A write cool-off is active with "
            f"{guards['cooloff_remaining_h']:.1f}h remaining. `pin` and `notes` "
            f"will refuse to start until it expires. Wait it out; do not delete "
            f"state/rate_ledger.json."
        )
    lock = guards.get("lock")
    if lock and lock.get("stale"):
        out.append(
            f"A ledger lock has been untouched for {lock['age_h']:.1f}h "
            f"(pid {lock.get('pid')}). If nothing is running, the next `pin` "
            f"will break it automatically -- no need to delete it by hand."
        )
    elif lock:
        out.append(
            f"Another run holds the write budget (pid {lock.get('pid')}, "
            f"{lock['age_h']:.1f}h). Wait for it rather than starting a second."
        )

    list_name = state.get("last_run", {}).get("map_title") or s.map_title
    csv_path = state.get("latest_csv")
    pins = state["pin_progress"]
    if csv_path and pins["ok"] == 0:
        out.append(
            f'To build a real Google Maps saved list, a human can run: pin '
            f'--csv "{csv_path}" --list "{list_name}" --limit 3 --json. It '
            "writes to the account and automates a UI that Google's terms do "
            "not permit, so it is never something to start unasked."
        )
    elif csv_path and pins["failed"]:
        out.append(
            f'{pins["failed"]} place(s) failed to pin. A human can retry just '
            f'those: pin --csv "{csv_path}" --list "{list_name}" --json.'
        )
    return out
