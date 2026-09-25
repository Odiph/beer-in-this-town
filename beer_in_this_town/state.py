"""Where is this project up to, and what should happen next?

This module exists so an agent never has to infer progress from log output or
guess whether a step already ran. `inspect_state()` reads the artifacts on disk
and reports a single, honest picture; `next_actions()` turns that into literal
commands to run.

That inversion is the point of the agent-led design: the tool owns the truth
about its own state, and the agent owns the judgement about what to do with it.
"""
from __future__ import annotations

import csv
import json
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import consent
from .app_sweep import journal_path as sweep_journal_path
from .config import (
    DEFAULT_METHOD,
    ENRICHED_CSV,
    EXCLUDED_CSV,
    EXPORT_STEM,
    STATE_DIR,
    SWEEP_CSV,
    SWEEP_METHODS,
    VENUES_CSV,
    Settings,
    cli_arg,
    scope_slug,
    stage_path,
)
from .guardrails import Limits, inspect_guardrails

LAST_RUN = STATE_DIR / "last_run.json"
LEGACY_PINNED = STATE_DIR / "pinned.json"


def record_run(query: str, map_title: str, csv_path: Path,
               method: str = "") -> None:
    """Remember the city and list the last `sweep` was for.

    `status` used to answer from `Settings()` defaults, because it is not one
    of the commands that derive settings from argv. So after `run --query
    london` it reported Singapore and handed an agent a literal `pin` command
    aiming a London CSV at a Singapore list -- a write to a live account, from
    the one command documented as read-only.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(
        json.dumps({"query": query, "map_title": map_title,
                    "csv": str(csv_path), "method": method}, indent=1),
        encoding="utf-8",
    )


def sweep_method(s: Settings) -> str:
    """The method the city's sweep uses: the last run's, the intent's, or
    the default. The same precedence `inspect_state` reports."""
    return (_read_json(LAST_RUN).get("method")
            or (last_intent() or {}).get("method") or DEFAULT_METHOD)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# The collection stages, in order. Each one reads the file the one before it
# wrote, so "done" is simply "its file is there and is not older than its
# input".
EXPORT_FORMATS = ("kml", "gpx", "geojson")
COLLECTION_STAGES = ("sweep", "enrich", "filter", "export")


def _csv_rows(path: Path) -> int | None:
    """Data rows in a CSV this project wrote, or None if it cannot be read."""
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            return max(sum(1 for _ in csv.reader(fh)) - 1, 0)
    except (OSError, csv.Error, UnicodeDecodeError):
        return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _file_stage(name: str, path: Path, source: Path | None) -> dict[str, Any]:
    """A stage whose artifact is one CSV. Stale when its input is newer."""
    exists = path.exists()
    stale = bool(exists and source is not None and source.exists()
                 and _mtime(source) > _mtime(path))
    count = _csv_rows(path) if exists else None
    if not exists:
        detail = f"not run: {path.name} is not there yet"
    elif stale:
        detail = (f"stale: {source.name if source else 'its input'} is newer "
                  f"than {path.name}; re-run {name}")
    else:
        detail = f"{count} row(s) in {path.name}"
    return {"name": name, "done": exists and not stale, "stale": stale,
            "count": count, "path": str(path), "detail": detail}


def _sweep_stage(city: str) -> dict[str, Any]:
    stage = _file_stage("sweep", stage_path(city, SWEEP_CSV), None)
    if not stage["done"]:
        # An interrupted sweep leaves its journal, and the next sweep resumes
        # from it. Say so, or a half-swept city reads as never started.
        journal = _read_json(sweep_journal_path(city))
        found = len(journal.get("venues", []) or [])
        if found:
            stage["detail"] = (f"in progress: {found} venue(s) in the sweep "
                               f"journal; `sweep` resumes from it")
            stage["journal_venues"] = found
    return stage


def _export_stage(city: str) -> dict[str, Any]:
    source = stage_path(city, VENUES_CSV)
    files = {fmt: stage_path(city, f"{EXPORT_STEM}.{fmt}")
             for fmt in EXPORT_FORMATS}
    present = {fmt: p for fmt, p in files.items() if p.exists()}
    newest = max((_mtime(p) for p in present.values()), default=0.0)
    stale = bool(present and source.exists() and _mtime(source) > newest)
    if not present:
        detail = "not run: no venues.kml/.gpx/.geojson yet"
    elif stale:
        detail = f"stale: {source.name} is newer than the map files; re-run export"
    else:
        detail = "wrote " + ", ".join(p.name for p in present.values())
    first = next(iter(present.values()), files["kml"])
    return {"name": "export", "done": bool(present) and not stale,
            "stale": stale, "count": len(present), "path": str(first),
            "files": {fmt: str(p) for fmt, p in present.items()},
            "detail": detail}


def _journal_stage(name: str, journal: dict, total: int | None,
                   scope: str) -> dict[str, Any]:
    """pin / notes progress, from the per-list journal those commands keep."""
    counts = Counter(str(v) for v in journal.values())
    done_n = counts.get("ok", 0)
    tally = {k.replace("-", "_"): n for k, n in sorted(counts.items())}
    if not scope:
        detail = "no list named yet"
    else:
        detail = (f"{done_n} of {total if total is not None else '?'} done "
                  f"for list {scope!r}")
    return {"name": name, "done": bool(total) and done_n >= (total or 0),
            "count": done_n, "total": total, "by_status": tally,
            "list": scope or None, "detail": detail}


def city_stages(city: str, list_name: str) -> list[dict[str, Any]]:
    """Per-city progress, read from data/<slug>/ and the list journals.

    No side effects, and nothing slower than reading a few small files.
    """
    sweep = _sweep_stage(city)
    enrich = _file_stage("enrich", stage_path(city, ENRICHED_CSV),
                         stage_path(city, SWEEP_CSV))
    filt = _file_stage("filter", stage_path(city, VENUES_CSV),
                       stage_path(city, ENRICHED_CSV))
    excluded = stage_path(city, EXCLUDED_CSV)
    if excluded.exists():
        filt["excluded"] = _csv_rows(excluded)
        filt["excluded_path"] = str(excluded)
    export = _export_stage(city)
    total = filt["count"]
    pinned = (_read_json(STATE_DIR / f"pinned_{scope_slug(list_name)}.json")
              if list_name else {})
    noted = (_read_json(STATE_DIR / f"noted_{scope_slug(list_name)}.json")
             if list_name else {})
    return [sweep, enrich, filt, export,
            _journal_stage("pin", pinned, total, list_name),
            _journal_stage("notes", noted, total, list_name)]


def next_stage(stages: list[dict[str, Any]]) -> str | None:
    """The first collection stage that has not produced a current file."""
    for stage in stages:
        if stage["name"] in COLLECTION_STAGES and not stage["done"]:
            return stage["name"]
    return None


def _stage(stages: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next((st for st in stages if st["name"] == name), {})


def inspect_state(s: Settings, *, probe_emulator: bool = False,
                  emulator: Callable[..., list] | None = None,
                  ) -> dict[str, Any]:
    """A complete, machine-readable picture of progress. No side effects.

    `probe_emulator` asks adb whether the emulator is ready -- and only when
    a sweep is the next stage, the one stage that needs it. It is off by
    default because the CLI also calls this just to recover the last city.
    """
    # A file, not a working account. `verify` is what settles that, and the
    # name is kept only because it is part of the published envelope.
    logged_in = s.storage_state.exists()
    verification = last_verification()

    last_run = _read_json(LAST_RUN)
    # A real run outranks an intention, which outranks the built-in default.
    # The middle one is what the dashboard writes, and is the whole reason
    # the city a person types reaches the agent at all.
    intent = last_intent() or {}
    query = last_run.get("query") or intent.get("query") or s.query or ""
    # Same precedence as the city. A run recorded before methods existed
    # carries none, and falls through to the default.
    method = (last_run.get("method") or intent.get("method")
              or DEFAULT_METHOD)
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

    stages = city_stages(query, list_name) if query else []
    upcoming = next_stage(stages) if query else None
    # The furthest stage CSV that exists: what pin/notes/label would read.
    best_csv = next((Path(st["path"]) for st in reversed(stages[:3])
                     if Path(st["path"]).exists()), None)
    export = _stage(stages, "export")

    checks = None
    if probe_emulator and upcoming == "sweep" and method == "map":
        if emulator is None:
            from .emulator_checks import check_emulator as emulator
        checks = [c.to_dict() for c in emulator(s.adb_serial)]

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
        "city": query or None,
        # How the next (or last) sweep collects venues: "search" or "map".
        # Only "map" needs the emulator, so only "map" probes it.
        "method": method,
        "city_dir": str(stage_path(query, "")) if query else None,
        "next_stage": upcoming,
        # Filled only when a sweep is next and the probe was asked for.
        # None means "not checked", never "fine".
        "emulator": checks,
        "latest_csv": str(best_csv) if best_csv else None,
        "latest_kml": (export.get("files") or {}).get("kml"),
        "venues_in_baseline": len(previous),
        "pin_progress": pin_counts,
        "pin_progress_scope": pin_scope,
        # The write guardrails, which `status` could not previously see even
        # though several error remedies send the caller here to check them.
        # `consent` is per list: what `allow-writes` recorded and how long
        # each grant has left. An empty list means `pin` and `notes` refuse.
        "write_guardrails": {**inspect_guardrails(Limits()),
                             "consent": consent.report()},
        "stages": stages,
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


def record_intent(query: str, map_title: str | None = None,
                  method: str | None = None) -> dict:
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
    if method is not None and method not in SWEEP_METHODS:
        raise BadIntent(f"Unknown method {method!r}; use one of "
                        f"{', '.join(SWEEP_METHODS)}.")
    payload = {"query": cleaned, "map_title": title, "at": time.time()}
    if method:
        payload["method"] = method
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


def _emulator_failed(state: dict[str, Any]) -> list[dict]:
    """The failing emulator checks, when they were run. [] when not run."""
    return [c for c in (state.get("emulator") or []) if not c.get("ok")]


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
    if state.get("next_stage") == "sweep" and _emulator_failed(state):
        # The sweep is next and the emulator is not ready: BlueStacks, adb,
        # the app and the display size are all a person's to set up.
        return "emulator"
    return None


def _stage_command(stage: str, city: str) -> str:
    return f"python -m beer_in_this_town {stage} --city {cli_arg(city)} --json"


def next_actions(state: dict[str, Any], s: Settings) -> list[str]:
    """Literal commands to run next. Ordered; the first one is the recommendation.

    Only read-only, safe commands appear here: status, doctor, verify, and
    the collection stages sweep / enrich / filter / export, none of which
    writes to an account. `pin` and `notes` write to the user's Google
    account and AGENTS.md says an agent must never run them without being
    asked -- while the loop this list defines says to run the first action
    and repeat until it is empty. Listing them made those two rules
    contradict each other, and the loop won. What a human might want to do
    next lives in `hints`, which nothing is instructed to execute.
    """
    query = state.get("last_run", {}).get("query") or s.query

    # Handing back `selfcheck` here, when nothing selfcheck does changes
    # `logged_in`, meant an agent following the loop in AGENTS.md -- run the
    # first action, re-read status, repeat -- ran it forever. An empty list
    # is the end of the loop, not an error; being blocked on a human IS the
    # end of the loop, and `data.blocked_on` says so in a field an agent can
    # branch on. The same rule is why an unready emulator yields [] rather
    # than `doctor`: doctor changes nothing, so offering it would loop.
    if not state["logged_in"]:
        # Opening the dashboard IS the next action, and an agent can do it:
        # `--detach` returns instead of blocking, and the page is read-only
        # with no route that can touch an account. Offered only while nothing
        # is serving -- re-launching a running dashboard forever is the same
        # non-termination as the `selfcheck` it replaced.
        from .ui import existing

        if existing() is None:
            return ["python -m beer_in_this_town ui --detach --json"]
        return []

    # `logged_in` means the file exists, which is not the same as the accounts
    # working. `verify` is cheap, touches no account, and terminates.
    verification = state.get("verification")
    if verification is None:
        return ["python -m beer_in_this_town verify --json"]
    if not verification.get("ok"):
        return []          # signed out: blocked_on says a person is needed

    if not query:
        # Nothing to offer: no stage has anywhere to go without a city, and
        # the dashboard is where a person names one.
        from .ui import existing

        if existing() is None:
            return ["python -m beer_in_this_town ui --detach --json"]
        return []

    stage = state.get("next_stage")
    if stage is None:
        # Every collection stage is done. What is left -- pin, notes,
        # closures -- writes to an account or spends money, so it is in
        # `hints`, and the loop ends here.
        return []
    if stage == "sweep" and _emulator_failed(state):
        return []          # blocked_on says "emulator"
    if stage == "sweep":
        # Explicit, so the command says which method it will use.
        method = state.get("method") or DEFAULT_METHOD
        return [f"python -m beer_in_this_town sweep --city {cli_arg(query)} "
                f"--method {method} --json"]
    return [_stage_command(stage, query)]


def _emulator_hints(state: dict[str, Any]) -> list[str]:
    failed = _emulator_failed(state)
    out = ["Blocked on a person: the emulator is not ready for a sweep. "
           "Fix these in order, then re-run `python -m beer_in_this_town "
           "doctor --json` to confirm:"]
    out += [f"{c['name']}: {c['detail']} Fix: {c['remedy']}" for c in failed]
    out.append("Then open the Untappd app on Discover -> View Map, and the "
               "sweep can start.")
    return out


def _write_hints(state: dict[str, Any], s: Settings) -> list[str]:
    """pin / notes / closures: exact commands, for a person to choose to run."""
    stages = state.get("stages") or []
    venues = _stage(stages, "filter")
    if not venues.get("done") and not venues.get("stale"):
        return []
    csv_path = cli_arg(venues["path"])
    list_name = state.get("last_run", {}).get("map_title") or s.map_title
    target = cli_arg(list_name) if list_name else '"<your list name>"'
    pin = _stage(stages, "pin")
    notes = _stage(stages, "notes")
    out = [
        f"Create the saved list {target} in Google Maps by hand (Saved -> "
        f"New list) before pinning; pin refuses a list that does not exist.",
    ]
    if not (list_name and consent.has_consent(list_name)):
        # Never a next action, and not something an agent can do: the
        # command refuses --json and a non-terminal stdin.
        out.append(
            f"pin and notes refuse to write without recorded consent for the "
            f"exact list. The person who owns the account runs, in their own "
            f"terminal: python -m beer_in_this_town allow-writes --list "
            f"{target} -- it explains what the writes cross and asks them to "
            f"type the list name to confirm.")
    by_status = pin.get("by_status") or {}
    if not pin.get("count"):
        out.append(
            f"To save the venues into that list, a human can run a trial of "
            f"three first: python -m beer_in_this_town pin --csv {csv_path} "
            f"--list {target} --limit 3 --json -- check them in Google Maps, "
            f"then run it again without --limit (100 a day at most, so a big "
            f"city takes several nights). It writes to the Google account and "
            f"automates a UI Google's terms do not permit, so it is never "
            f"started unasked.")
    elif not pin.get("done") or by_status.get("failed"):
        out.append(
            f"{pin['count']} place(s) saved so far"
            + (f", {by_status['failed']} failed" if by_status.get("failed")
               else "")
            + f". A human can continue (it resumes and skips what is done): "
              f"python -m beer_in_this_town pin --csv {csv_path} --list "
              f"{target} --json")
    if pin.get("count") and not notes.get("done"):
        out.append(
            f"To write the Untappd stats into each saved place's note, a human "
            f"can run: python -m beer_in_this_town notes --csv {csv_path} "
            f"--list {target} --limit 3 --json, then again without --limit.")
    out.append(
        f"Optional, and billed to your Google Places key: python -m "
        f"beer_in_this_town closures --csv {csv_path} --limit 3 --json flags "
        f"venues Google believes have closed. Never run unasked.")
    return out


def hints(state: dict[str, Any], s: Settings) -> list[str]:
    """What a person might want to do next. Never executed by anything."""
    # Keyed off `blocked_on`, not off the session file. A tested-and-expired
    # session sets `blocked_on` to sign_in while `logged_in` stays true.
    blocked = blocked_on(state)
    if blocked == "choose_city":
        return [
            "No city chosen, and there is no default -- picking one for "
            "someone is picking what they get. Open `beertown ui` and name a "
            "city on the last step, or pass `sweep --city \"<city>\"`.",
        ] + ([f"The dashboard is already open at {running['url']}."]
             if (running := _serving()) else [])

    if blocked == "sign_in":
        expired = bool(state.get("verification"))
        return [
            ("A saved session is present and was tested: one of the accounts "
             "is signed out. " if expired else "")
            + "Blocked on a person: this needs a password, so no agent can do "
            "it. Run `beertown ui` yourself -- it opens a "
            "dashboard that signs you in and then tests both accounts "
            "for real, rather than trusting a cookie means they work.",
            "Both accounts matter. Google is where the saved list lives; "
            "Untappd on the web is what `enrich` reads venue pages through.",
            "Once that is done, `python -m beer_in_this_town verify --json` "
            "checks both accounts for real and needs no browser -- an agent "
            "can run it to find out whether the sign-in actually took.",
        ] + ([f"The dashboard is already open at {running['url']} — send them "
              f"there rather than starting another."]
             if (running := _serving()) else [])

    if blocked == "emulator":
        return _emulator_hints(state)

    out: list[str] = []
    if state.get("next_stage") == "sweep" and state.get("method") == "map":
        out.append(
            "Before the sweep: open the Untappd app in BlueStacks on Discover "
            "-> View Map. The sweep reads that map, and reads only.")
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
    return out + _write_hints(state, s)
