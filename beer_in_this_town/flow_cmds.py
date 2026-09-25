"""The v0.2 flow after the sweep: `enrich`, `filter`, `export`.

One command per step, each reading the previous step's file for a city:

    data/<slug>/1_sweep.csv  --enrich-->  2_enriched.csv
                             --filter-->  3_venues.csv + 3_excluded.csv
                             --export-->  venues.kml / .gpx / .geojson

`--in PATH` overrides the input; the output always lands in the city's
folder, so the next step finds it. A missing input is `stage_input_missing`
with the exact command that makes it -- never an empty output that looks
like an empty city.

`3_venues.csv` carries every column `pin --csv` and `notes --csv` read
(`name`, `address`, `city`, `total`, `unique`, `monthly`), so the last two
steps take it as it is. Those two write to a Google account and appear only
in `hints`, never in `next_actions`.

`cli.py` owns the parser and the exception mapping: it calls `add_parsers`
and `dispatch`, and the deliberate stops enrich can raise
(`RateLimitTripped`, `BudgetExceeded`, `TransportUnavailable`) reach its
handlers unchanged.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from . import config
from .agent_io import Envelope, Problem, fail
from .classify import craft_beer_decision
from .config import Settings, city_slug, cli_arg, stage_path
from .export import (
    MAP_FORMATS,
    osm_attribution,
    write_geojson,
    write_gpx,
    write_kml,
)
from .models import CSV_FIELDS, Venue, VenueRef
from .parsers import StatsLoginRequired

log = logging.getLogger(__name__)

PY = "python -m beer_in_this_town"

SWEEP_CSV = "1_sweep.csv"
ENRICHED_CSV = "2_enriched.csv"
VENUES_CSV = "3_venues.csv"
EXCLUDED_CSV = "3_excluded.csv"
MAP_BASENAME = "venues"

ENRICHED_FIELDS = [*CSV_FIELDS, "sweep_name", "resolution", "resolution_detail"]
FILTERED_FIELDS = [*ENRICHED_FIELDS, "kind", "flag", "reason"]

COMMANDS = ("enrich", "filter", "export")

Search = Callable[[str], list[VenueRef]]
Fetch = Callable[[VenueRef], Venue]


# --- parser ---------------------------------------------------------------

def add_parsers(sub: argparse._SubParsersAction,
                common: argparse.ArgumentParser) -> None:
    """Register `enrich`, `filter` and `export` on the CLI's subparsers."""
    helps = {
        "enrich": "join each swept name to its Untappd venue page "
                  "(1_sweep.csv -> 2_enriched.csv)",
        "filter": "keep craft-beer venues, list the rest with a reason "
                  "(2_enriched.csv -> 3_venues.csv + 3_excluded.csv)",
        "export": "write map files from 3_venues.csv (kml, gpx, geojson)",
    }
    for name in COMMANDS:
        p = sub.add_parser(name, parents=[common], help=helps[name])
        p.add_argument("--city", default=None,
                       help="the city whose data/<slug>/ files to use. "
                            "No default.")
        p.add_argument("--in", dest="in_path", default=None,
                       help="read this file instead of the previous step's")
        if name == "enrich":
            p.add_argument("--limit", type=_positive, default=None,
                           help="look up at most this many venues not done "
                                "yet, then stop; re-run to continue. Progress "
                                "is saved after every venue either way.")
        if name == "export":
            p.add_argument("--format", default=",".join(MAP_FORMATS),
                           help="comma-separated: kml, gpx, geojson "
                                "(default: all three)")


def dispatch(args: argparse.Namespace, s: Settings) -> Envelope | None:
    """Run one of this module's commands, or return None if it is not ours."""
    cmd = getattr(args, "cmd", None)
    if cmd not in COMMANDS:
        return None
    city = (getattr(args, "city", None) or s.query or "").strip()
    if not city:
        return fail(cmd, Problem(
            code="no_city",
            message="No city given, and none has been chosen.",
            remedy='Pass --city "<city>" -- the same city you swept. There '
                   "is deliberately no default.",
        ))
    in_path = getattr(args, "in_path", None)
    if cmd == "enrich":
        return cmd_enrich(s, city, in_path, limit=getattr(args, "limit", None))
    if cmd == "filter":
        return cmd_filter(s, city, in_path)
    return cmd_export(s, city, in_path, getattr(args, "format", None))


# --- stage files ----------------------------------------------------------

def _cmd(step: str, city: str) -> str:
    return f'{PY} {step} --city {cli_arg(city)} --json'


def _input(command: str, city: str, in_path: str | None, default: str,
           producer: str) -> Path | Envelope:
    path = Path(in_path) if in_path else stage_path(city, default)
    if path.is_file():
        return path
    return fail(command, Problem(
        code="stage_input_missing",
        message=f"{path} does not exist. `{command}` reads the output of "
                f"`{producer}`.",
        remedy=_cmd(producer, city),
    ), input=str(path))


def read_stage(path: Path) -> list[tuple[Venue, dict[str, str]]]:
    """Each row as a `Venue`, with the raw row for the extra columns."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("name") or "").strip()]
    return [(Venue.from_row(r), r) for r in rows]


def write_stage(path: Path, rows: list[dict], fields: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" or Windows writes a blank line after every row.
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %d rows -> %s", len(rows), path)
    return path


# --- enrich ---------------------------------------------------------------

def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


# --- enrich journal -------------------------------------------------------
#
# A city is hours of paced lookups, and a run can be stopped at any point --
# found live: London was stopped three times, once at venue 391 of 629, with
# nothing written, because the CSV is written only at the end. So every venue
# is journalled as it resolves, keyed by its position in the sweep file and
# tied to that file's content: a new sweep starts a new journal.

def _journal_path(city: str) -> Path:
    return config.STATE_DIR / f"enriched_{city_slug(city)}.json"


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_journal(city: str, source: str) -> dict[str, dict]:
    try:
        raw = json.loads(_journal_path(city).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if raw.get("source") != source:
        return {}
    return dict(raw.get("done", {}))


def _save_journal(city: str, source: str, done: dict[str, dict]) -> None:
    path = _journal_path(city)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"source": source, "done": done},
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def cmd_enrich(s: Settings, city: str, in_path: str | None = None, *,
               limit: int | None = None,
               search: Search | None = None,
               fetch: Fetch | None = None) -> Envelope:
    """Join every swept venue to its venue page; keep the ones that do not.

    Resumable: each venue is journalled as it resolves, so a stopped run
    loses nothing and a re-run starts where it stopped. `--limit N` looks up
    at most N venues not done yet and stops -- short batches, each of which
    finishes. `2_enriched.csv` is written only once every venue is done, so
    `filter` never runs on part of a city.

    `search` and `fetch` are injected by tests. Left out, the real ones are
    used: Chrome for the per-name search, `PoliteClient` for the pages, both
    on the same paced, on-disk hourly budget.
    """
    src = _input("enrich", city, in_path, SWEEP_CSV, "sweep")
    if isinstance(src, Envelope):
        return src
    swept = [v for v, _ in read_stage(src)]
    source = _fingerprint(src)
    done = _load_journal(city, source)
    todo = [i for i in range(len(swept)) if str(i) not in done]
    batch = todo[:limit] if limit else todo

    def record(i: int, rows: list, report) -> None:
        res = report.resolutions[0]
        row = ({**rows[0][0].to_row(), "sweep_name": res.name,
                "resolution": res.status,
                "resolution_detail": res.detail} if rows else None)
        done[str(i)] = {"status": res.status, "row": row}
        _save_journal(city, source, done)

    try:
        if search is not None and fetch is not None:
            _enrich_batch(swept, batch, city, search, fetch, record)
        else:
            stopped = _enrich_live(s, swept, batch, city, record)
            if stopped is not None:
                return stopped
    except StatsLoginRequired as exc:
        return fail("enrich", Problem(
            code="not_signed_in",
            message=f"The Untappd session is signed out or expired ({exc}).",
            remedy=_SIGN_IN_REMEDY))

    remaining = len(todo) - len(batch)
    counts = Counter(entry["status"] for entry in done.values())
    base = {"city": city, "input": str(src), "venues": len(swept),
            "done": len(done), "remaining": remaining,
            "resolved": counts.get("resolved", 0), "statuses": dict(counts),
            "journal": str(_journal_path(city))}
    if remaining:
        again = (f"{PY} enrich --city {cli_arg(city)} --limit {limit} --json"
                 if limit else _cmd("enrich", city))
        return Envelope(
            command="enrich", ok=True, data={**base, "csv": None},
            warnings=[f"{remaining} venue(s) still to look up; "
                      f"{ENRICHED_CSV} is written when all are done."],
            next_actions=[again],
        )

    rows, dupes = _journal_rows(done, len(swept))
    out = write_stage(stage_path(city, ENRICHED_CSV), rows, ENRICHED_FIELDS)
    unresolved = sum(1 for r in rows if r["resolution"] != "resolved")
    warnings = []
    if not swept:
        warnings.append(f"{src} has no venues; nothing to enrich.")
    if unresolved:
        warnings.append(
            f"{unresolved} venue(s) did not join to a venue page and are kept "
            f"with unknown counts; `resolution` in {out.name} says why.")
    return Envelope(
        command="enrich", ok=True,
        data={**base, "csv": str(out), "rows": len(rows),
              "duplicates": dupes},
        warnings=warnings,
        next_actions=[_cmd("filter", city)],
    )


def _journal_rows(done: dict[str, dict], total: int) -> tuple[list[dict], int]:
    """The journal as CSV rows, in sweep order, one row per venue page.

    Two swept names can land on one page -- the app lists `Oscar Wilde` and
    `Oscar Wilde - Irish Pub` separately -- and batches cannot see each
    other, so the duplicate is dropped here, the first one kept.
    """
    rows, seen, dupes = [], set(), 0
    for i in range(total):
        row = (done.get(str(i)) or {}).get("row")
        if row is None:
            continue
        vid = (row.get("venue_id") or "").strip()
        if vid and row.get("resolution") == "resolved":
            if vid in seen:
                dupes += 1
                continue
            seen.add(vid)
        rows.append(row)
    return rows, dupes


def _enrich_batch(swept: list[Venue], batch: list[int], city: str,
                  search: Search, fetch: Fetch, record, rest=None) -> None:
    from .city_names import cached
    from .resolve import enrich_rows

    # A search sweep's rows are placed from their pages; the city's bounds,
    # cached when the sweep looked up its name variants, drop the namesakes.
    names = cached(city)
    within = names.contains if names is not None else None
    if within is None and any(swept[i].ref.url and not swept[i].has_coords
                              for i in batch):
        log.warning("No cached bounds for %s: search-sweep venues are not "
                    "checked for being outside the city. Re-run the sweep to "
                    "rebuild them.", city)
    for n, i in enumerate(batch, 1):
        rows, report = enrich_rows([swept[i]], city, search, fetch,
                                   within=within)
        record(i, rows, report)
        if rest is not None and n < len(batch):
            rest(n)


_SIGN_IN_REMEDY = (
    "Ask the human to sign in to untappd.com: run `beertown ui` and use the "
    "Accounts step (it needs their password, so an agent cannot do it). Then "
    "`verify --json`, then re-run this command; the venues already looked up "
    "are kept and are not looked up again.")


def _enrich_live(s: Settings, swept: list[Venue], batch: list[int],
                 city: str, record) -> Envelope | None:
    """The real search and fetch, with the robots check `run` had."""
    from .http_client import PoliteClient, _cookies_from_storage_state
    from .resolve import BrowserNameSearch, ReadingRhythm, page_fetcher

    if not batch:
        return None
    if not any(swept[i].has_coords or swept[i].ref.url for i in batch):
        # Nothing can be matched without a pin or a known page; do not open
        # a browser for it.
        _enrich_batch(swept, batch, city, lambda _q: [], _no_fetch, record)
        return None
    # Signed out, Untappd hides the stats on every unverified venue page --
    # measured: 4 of 7 Tel Aviv pages. Refuse before spending one request.
    if not _cookies_from_storage_state(s.storage_state, "untappd.com"):
        return fail("enrich", Problem(
            code="not_signed_in",
            message="No Untappd session: signed out, Untappd shows venue "
                    "stats only for verified venues, so most of the map "
                    "would come back without numbers.",
            remedy=_SIGN_IN_REMEDY))
    with PoliteClient(s) as client:
        if s.respect_robots and client.robots_disallows_scraping():
            return fail("enrich", Problem(
                code="robots_disallow",
                message="Untappd's robots.txt disallows the venue pages.",
                remedy="Stop and ask a human. Nothing was fetched.",
            ))
        # One budget for the searches and the page fetches.
        with BrowserNameSearch(s, budget=client._budget) as search:
            _enrich_batch(swept, batch, city, search, page_fetcher(client),
                          record, rest=ReadingRhythm(
                              requests_used=client._budget.used))
    return None


def _no_fetch(ref: VenueRef) -> Venue:  # pragma: no cover - never reached
    raise RuntimeError("no fetch without a pin")


# --- filter ---------------------------------------------------------------

def cmd_filter(s: Settings, city: str, in_path: str | None = None) -> Envelope:
    """Split enriched rows into craft-beer venues and the rest, with reasons."""
    src = _input("filter", city, in_path, ENRICHED_CSV, "enrich")
    if isinstance(src, Envelope):
        return src

    kept: list[dict] = []
    excluded: list[dict] = []
    reasons: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    for venue, raw in read_stage(src):
        d = craft_beer_decision(venue)
        row = {**raw, **venue.to_row(), "kind": d.kind, "flag": d.flag,
               "reason": d.reason}
        (kept if d.keep else excluded).append(row)
        if not d.keep:
            reasons[d.reason] += 1
        elif d.flag:
            flags[d.flag] += 1

    venues_csv = write_stage(stage_path(city, VENUES_CSV), kept,
                             FILTERED_FIELDS)
    excluded_csv = write_stage(stage_path(city, EXCLUDED_CSV), excluded,
                               FILTERED_FIELDS)
    warnings = []
    if flags:
        warnings.append(
            f"{sum(flags.values())} kept venue(s) are flagged "
            f"({', '.join(f'{k}: {n}' for k, n in sorted(flags.items()))}). "
            f"Flagged, not dropped: removing one is your decision.")
    uncategorised = sum(1 for r in kept if r["kind"] == "uncategorised")
    if uncategorised:
        warnings.append(
            f"{uncategorised} kept venue(s) have no category (not joined to a "
            f"venue page); they passed the app's drinking filter only.")
    return Envelope(
        command="filter", ok=True,
        data={"city": city, "input": str(src), "csv": str(venues_csv),
              "excluded_csv": str(excluded_csv), "kept": len(kept),
              "excluded": len(excluded), "excluded_by_reason": dict(reasons),
              "flagged": dict(flags)},
        warnings=warnings,
        next_actions=[_cmd("export", city)],
        hints=[f"Optional, paid (Google Places key): a human can check "
               f"closures with: {PY} closures --csv {cli_arg(str(venues_csv))} "
               f"--limit 3 --json"],
    )


# --- export ---------------------------------------------------------------

def parse_formats(raw: str | None) -> tuple[str, ...]:
    asked = tuple(f.strip().lower() for f in (raw or "").split(",") if f.strip())
    unknown = [f for f in asked if f not in MAP_FORMATS]
    if unknown or not asked:
        raise ValueError(f"Unknown or empty --format {raw!r}; choose from "
                         f"{', '.join(MAP_FORMATS)}.")
    return tuple(dict.fromkeys(asked))


def cmd_export(s: Settings, city: str, in_path: str | None = None,
               formats: str | None = None) -> Envelope:
    """Map files from 3_venues.csv, crediting OSM when its data is in them."""
    try:
        wanted = parse_formats(formats or ",".join(MAP_FORMATS))
    except ValueError as exc:
        return fail("export", Problem(
            code="bad_format", message=str(exc),
            remedy=f'{PY} export --city {cli_arg(city)} --format '
                   f'{",".join(MAP_FORMATS)} --json',
        ))
    src = _input("export", city, in_path, VENUES_CSV, "filter")
    if isinstance(src, Envelope):
        return src

    venues = [v for v, _ in read_stage(src)]
    credit = osm_attribution(venues)
    title = f"{city} craft beer"
    written: dict[str, str] = {}
    for fmt in wanted:
        path = stage_path(city, f"{MAP_BASENAME}.{fmt}")
        if fmt == "kml":
            write_kml(venues, path, title, attribution=credit)
        elif fmt == "gpx":
            write_gpx(venues, path, title, attribution=credit)
        else:
            write_geojson(venues, path, attribution=credit)
        written[fmt] = str(path)

    unplaced = sum(1 for v in venues if not v.has_coords)
    warnings = ([f"{unplaced} venue(s) have no coordinates and are not in the "
                 f"map files (they are still in {src.name})."]
                if unplaced else [])
    return Envelope(
        command="export", ok=True,
        data={"city": city, "input": str(src), "files": written,
              "placemarks": len(venues) - unplaced, "unplaced": unplaced,
              "attribution": credit},
        warnings=warnings,
        next_actions=[f"{PY} status --json"],
        hints=[
            "In Google Maps (a person, by hand): Saved -> New list, and name "
            "it. The tool never creates or guesses a list.",
            f"Then a person can trial-pin 3 places into it: {PY} pin --csv "
            f"{cli_arg(str(src))} --list \"<your list name>\" --limit 3 --json -- check "
            f"them in Google Maps before pinning the rest (100/day budget).",
            f"After pinning, check-in stats go into each place's note with: "
            f"{PY} notes --csv {cli_arg(str(src))} --list \"<your list name>\" "
            f"--limit 3 --json",
        ],
    )
