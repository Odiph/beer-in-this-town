"""Command-line entry point, designed to be driven by a coding agent.

Every command accepts --json and then prints exactly one envelope on stdout
(see agent_io.py). Logs go to stderr. Failures still print a valid envelope
carrying a machine-readable error code and a remedy, so an agent can recover
without parsing tracebacks.

  python -m beer_in_this_town status --json      # where am I, what is next
  python -m beer_in_this_town doctor --json      # are the preconditions met
  python -m beer_in_this_town bootstrap          # one-time interactive login
  python -m beer_in_this_town selfcheck --json   # 1 request: are selectors alive
  python -m beer_in_this_town run --no-upload --json  # collect -> CSV + maps
  python -m beer_in_this_town pin  --json        # save into a Google Maps list
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

from .agent_io import Envelope, Problem, emit, fail, log_to_stderr
from .config import DATA_DIR, SEARCH_URL, Settings, ensure_dirs
from .export import (
    commit_run,
    diff_against_previous,
    today_stamp,
    write_csv,
    write_diff_outputs,
    write_geojson,
    write_gpx,
    write_kml,
)
from .geocode import GeocoderUnavailable, geocode_missing
from .guardrails import AlreadyRunning, Tripped
from .http_client import (
    BudgetExceeded,
    PoliteClient,
    RateLimitTripped,
    TransportUnavailable,
)
from .measure import (
    DEFAULT_QUOTA,
    LabelsUnusable,
    PartialStratum,
    read_sheet,
    score_labels,
    stratified_sample,
    venues_from_csv,
    write_sheet,
)
from .models import VenueRef
from .mymaps_upload import manual_instructions, upload_kml
from .notes import MAX_GAP_S as NOTES_MAX_GAP
from .notes import MIN_GAP_S as NOTES_MIN_GAP
from .notes import add_notes, notes_from_csv
from .parsers import (
    ClientRenderedSearch,
    ParseError,
    assert_corpus_quality,
    parse_search_page,
    parse_venue_stats,
)
from .pin_to_list import MAX_GAP_S as PIN_MAX_GAP
from .pin_to_list import MIN_GAP_S as PIN_MIN_GAP
from .pin_to_list import (
    AmbiguousList,
    pin_places,
    places_from_csv,
    region_from_csv,
)
from .places import PlacesUnavailable, resolve_closures
from .places import counts as closure_counts
from .scrape import SearchLoginRequired, collect_venue_refs, fetch_venues
from .state import (
    blocked_on,
    hints,
    inspect_state,
    next_actions,
    record_run,
)
from .ui.server import DEFAULT_PORT as UI_DEFAULT_PORT

log = logging.getLogger("beer_in_this_town")


class EnvelopeParser(argparse.ArgumentParser):
    """An argparse parser that still honours the envelope contract.

    argparse writes usage to stderr and exits 2, so a rejected flag produced
    no envelope at all -- and the contract says every invocation prints
    exactly one. An agent got an unparseable blob and no `error.code` to
    branch on, which for a rejected *pacing* flag is the worst case: the
    remedy is to stop, and a caller with nothing to read may simply retry.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        as_json = "--json" in sys.argv
        emit(fail(
            _requested_command(),
            Problem(
                code="bad_arguments",
                message=message,
                remedy=f"Fix the argument and re-run. "
                       f"`{self.prog} --help` lists the valid ones.",
            ),
        ), as_json)
        raise SystemExit(2)


def _requested_command() -> str:
    """The subcommand from argv, for an envelope built before parsing finished."""
    return next((a for a in sys.argv[1:] if a in _SUBCOMMANDS),
                "beer-in-this-town")


def at_least(floor: float, what: str):
    """An argparse type that refuses to go below a floor.

    The pacing numbers are account-safety guardrails, and AGENTS.md rule 4
    says not to lower them. Prose is not a guardrail: every other protection
    in this project fails closed, and these could be switched off with a flag
    by anyone -- or any agent -- who had not read the rule.

    Raising them is always allowed. A throttled user is explicitly told to.
    """
    def parse(raw: str) -> float:
        value = float(raw)
        if value < floor:
            raise argparse.ArgumentTypeError(
                f"{what} must be at least {floor:g}s. It paces requests so "
                f"this does not look like a script; lowering it is what gets "
                f"an account flagged. Raise it if you are being throttled."
            )
        return value
    return parse


def check_pacing(min_gap: float, max_gap: float) -> None:
    """Refuse a range that is not a range."""
    if max_gap < min_gap:
        raise ValueError(
            f"--max-gap ({max_gap:g}s) is below --min-gap ({min_gap:g}s). "
            f"The gap is drawn from that range, so an inverted one paces on "
            f"nonsense."
        )


MAP_FORMATS = ("kml", "geojson", "gpx")


def parse_formats(raw: str) -> tuple[str, ...]:
    """Validate --format at the boundary rather than writing nothing silently."""
    asked = tuple(f.strip().lower() for f in raw.split(",") if f.strip())
    unknown = [f for f in asked if f not in MAP_FORMATS]
    if unknown:
        raise ValueError(
            f"Unknown map format(s): {', '.join(unknown)}. "
            f"Choose from {', '.join(MAP_FORMATS)}."
        )
    if not asked:
        raise ValueError(f"--format needs at least one of {', '.join(MAP_FORMATS)}.")
    return asked


def setup_logging(verbose: bool, as_json: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    if as_json:
        log_to_stderr()


# --------------------------------------------------------------------------
# Agent-facing introspection
# --------------------------------------------------------------------------
def cmd_status(s: Settings) -> Envelope:
    state = inspect_state(s)
    # Additive field, so no schema bump -- AGENTS.md says `data` gains keys
    # without one. It tells an empty `next_actions` that means "finished"
    # apart from one that means "waiting for a person".
    state = {**state, "blocked_on": blocked_on(state)}
    return Envelope(
        command="status",
        ok=True,
        data=state,
        next_actions=next_actions(state, s),
        hints=hints(state, s),
    )


def cmd_verify(s: Settings) -> Envelope:
    """Test both accounts for real. Read-only, terminates, agent-safe.

    The dashboard blocks on a person, which is right for a sign-in and wrong
    for everything else: an agent still needs to know whether the sign-in
    took, and had no way to ask. This is that question, with an envelope.

    It opens a headless browser and makes one request. It writes nothing,
    touches no saved list, and always returns.
    """
    from .ui.checks import verify_google, verify_untappd

    results = {}
    for name, verifier in (("google", verify_google),
                           ("untappd", verify_untappd)):
        r = verifier(s)
        results[name] = {"ok": r.ok, "ran": r.ran, "detail": r.detail,
                         "evidence": r.evidence}

    failed = [k for k, v in results.items() if not v["ok"]]
    # A probe that could not run is not a signed-out account, and the two
    # want opposite remedies. Never collapse them -- see VerifyResult.ran.
    unran = [k for k, v in results.items() if not v["ran"]]

    if unran:
        return fail("verify", Problem(
            code="verify_unavailable",
            message="Could not test " + " and ".join(unran) + ": "
                    + "; ".join(results[k]["evidence"] for k in unran),
            remedy="This is not a signed-out account -- the check itself "
                   "could not run. Fix what the message names (usually a "
                   "missing browser) and re-run.",
        ), accounts=results)

    if failed:
        return fail("verify", Problem(
            code="not_signed_in",
            message="Signed out of " + " and ".join(failed) + ".",
            remedy="Ask the human to run `beertown ui` and sign in; it needs "
                   "a password, so an agent cannot do it. Signed out of "
                   "Untappd, search stops at 5 results and a run would build "
                   "a five-venue corpus.",
        ), accounts=results)

    return Envelope(
        command="verify", ok=True,
        data={"accounts": results, "verified": True},
        next_actions=["python -m beer_in_this_town status --json"],
    )


def cmd_doctor(s: Settings) -> Envelope:
    """Check preconditions without touching the network more than necessary."""
    problems: list[str] = []
    data: dict[str, object] = {}

    try:
        import httpx  # noqa: F401
        data["httpx"] = "ok"
    except ImportError:
        problems.append("httpx missing -- pip install -e \".[browser]\"")
    try:
        import bs4  # noqa: F401
        data["beautifulsoup4"] = "ok"
    except ImportError:
        problems.append("beautifulsoup4 missing -- pip install -e \".[browser]\"")
    try:
        import playwright  # noqa: F401
        data["playwright"] = "ok"
    except ImportError:
        problems.append(
            "playwright missing (only needed for bootstrap/pin) "
            "-- pip install -e \".[browser]\""
        )

    # "present", not "logged in". The file existing says a login happened
    # once, not that the account still works -- and reporting the stronger
    # claim is how a dead session reaches a hundred-request run. `beertown
    # ui` is what actually tests them.
    data["session_file"] = ("present (untested)"
                            if s.storage_state.exists() else "missing")
    data["profile_dir"] = "present" if s.profile_dir.exists() else "missing"
    notes: list[str] = []
    if not s.storage_state.exists():
        problems.append("no saved session -- run: beertown ui")
    else:
        # A note, not a problem: an untested session is not a broken one, and
        # making `doctor` permanently red would train everyone to ignore it.
        notes.append(
            "The saved session has not been tested. Run `beertown ui` to "
            "check both accounts actually work -- a cookie on disk is not a "
            "working login, and a dead one only shows up mid-run."
        )

    data["geocoder"] = "google" if s.google_geocoding_key else "nominatim (free, 1 req/s)"

    return Envelope(
        command="doctor",
        ok=not problems,
        data=data,
        warnings=problems,
        hints=notes,
        next_actions=["python -m beer_in_this_town status --json"],
    )


# --------------------------------------------------------------------------
# Pipeline commands
# --------------------------------------------------------------------------
def cmd_bootstrap(s: Settings, timeout_s: float = 900.0,
                  capture_only: bool = False) -> Envelope:
    """One-time login, in a browser Google is willing to accept.

    Google refuses to complete a sign-in inside an automation-controlled
    browser: "Couldn't sign you in -- This browser or app may not be secure."
    Playwright-launched Chrome always carries those automation flags, so the
    login can never happen there.

    So we split it: a plain Chrome process (no Playwright, no automation flags)
    handles the login into our own profile directory, and Playwright reuses the
    resulting session afterwards. Google blocks the sign-in *flow*, not an
    existing session.
    """
    from playwright.sync_api import sync_playwright

    from .chrome_launch import (
        find_chrome,
        launch_for_login,
        profile_has_google_session,
    )

    if capture_only:
        if not s.profile_dir.exists():
            return fail("bootstrap", Problem(
                code="no_profile",
                message=f"No profile at {s.profile_dir}.",
                remedy="python -m beer_in_this_town bootstrap",
            ))
        return _capture_session(s, sync_playwright)

    if find_chrome() is None:
        return fail("bootstrap", Problem(
            code="chrome_not_found",
            message="Could not find a Google Chrome installation.",
            remedy="Install Google Chrome, or log in manually: launch Chrome "
                   f'with --user-data-dir="{s.profile_dir}", sign in, close it, '
                   "then re-run bootstrap.",
        ))

    proc = launch_for_login(s.profile_dir)
    if proc is None:  # pragma: no cover -- guarded by find_chrome above
        return fail("bootstrap", Problem(
            code="chrome_not_found",
            message="Chrome could not be started.",
            remedy="Launch it by hand with the profile directory above.",
        ))

    print(
        "\nA normal Chrome window is opening -- not an automated one, which is\n"
        "the whole point: Google refuses logins in automation-controlled\n"
        "browsers.\n\n"
        "  1. Sign in to your Google account in that window.\n"
        "  2. While you are there, log in to untappd.com too, so the YOU\n"
        "     check-in column gets populated.\n"
        "  3. CLOSE the Chrome window when you are done.\n\n"
        "Closing it is the signal that you have finished. This then captures\n"
        "the session and exits.\n",
        file=sys.stderr,
    )

    # Poll while waiting so we can tell the user the moment the login lands,
    # rather than leaving them guessing whether it worked.
    deadline = time.time() + timeout_s
    announced = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break  # window closed: the agreed finish signal
        if not announced and profile_has_google_session(s.profile_dir):
            announced = True
            print(
                "\n  Google session detected. Close the Chrome window to "
                "finish.\n",
                file=sys.stderr,
            )
        time.sleep(5)
    else:
        # Timed out with Chrome still open. Do NOT kill it -- the user may be
        # mid-login, and killing Chrome can corrupt the profile.
        return fail("bootstrap", Problem(
            code="login_timed_out",
            message=f"Chrome was still open after {timeout_s / 60:.0f} minutes.",
            remedy="Close the Chrome window, then run: "
                   "python -m beer_in_this_town bootstrap --capture",
        ))

    return _capture_session(s, sync_playwright)


def _capture_session(s: Settings, sync_playwright) -> Envelope:
    """Read the session out of a profile Chrome has finished writing.

    Requires Chrome to be closed: it holds an exclusive lock on the profile.
    """
    time.sleep(2)  # let Chrome flush its cookie store to disk
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(s.profile_dir),
            channel="chrome",
            headless=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.google.com/maps", wait_until="domcontentloaded",
                  timeout=60_000)
        page.wait_for_timeout(5000)
        signed_in = page.locator(
            "a[aria-label*='Google Account'], img[alt*='Google Account']"
        ).count() > 0
        if signed_in:
            ctx.storage_state(path=str(s.storage_state))
        ctx.close()

    if not signed_in:
        return fail("bootstrap", Problem(
            code="login_not_detected",
            message="Chrome closed, but no Google session was found in the "
                    "profile.",
            remedy="Re-run bootstrap and make sure you complete the Google "
                   "sign-in before closing the window.",
        ))

    return Envelope(
        command="bootstrap",
        ok=True,
        data={"storage_state": str(s.storage_state),
              "profile_dir": str(s.profile_dir)},
        next_actions=["python -m beer_in_this_town status --json"],
    )


def _probe_search(client, s: Settings) -> str:
    """Is the search page one of the two shapes we know how to handle?

    Either it carries `.beer-item` rows (server-rendered) or an `#algolia-hits`
    container the browser path can fill (client-rendered). Anything else is a
    stale selector, and saying so is the entire reason this probe exists:
    search moved to Algolia, every `run` broke, and `selfcheck` stayed green
    for the duration because venue detail pages were never affected.

    Deliberately says nothing about how many results came back. Anonymous
    search is capped at five by Untappd's login gate, so a count assertion
    would fail on a healthy signed-out install.
    """
    html = client.get(SEARCH_URL, params={"q": s.query, "type": "venues"},
                      use_cache=False)
    try:
        parse_search_page(html)
    except ClientRenderedSearch:
        return "client-rendered"
    return "server-rendered"


def cmd_selfcheck(s: Settings, slug: str, venue_id: str,
                  probe_search: bool = True) -> Envelope:
    """Cheap pre-flight: one venue page and one search page, shape asserted."""
    ref = VenueRef(venue_id=venue_id, slug=slug, name="selfcheck",
                   category=None, address=None, city=None)
    search_shape = "not checked"
    try:
        with PoliteClient(s) as client:
            html = client.get(ref.url, use_cache=False)
            venue = parse_venue_stats(html, ref)
            if probe_search:
                search_shape = _probe_search(client, s)
    except ParseError as exc:
        return fail("selfcheck", Problem(
            code="selectors_stale",
            message=str(exc),
            remedy="Inspect the dumped HTML in debug/ and update the selectors "
                   "in beer_in_this_town/parsers.py",
        ))
    except Exception as exc:
        return fail("selfcheck", Problem(
            code="fetch_failed",
            message=str(exc),
            remedy="Check connectivity, then retry. Repeated 403s mean a block.",
        ))

    if not venue.has_public_stats:
        return fail("selfcheck", Problem(
            code="stats_missing",
            message="Known-good venue page yielded no stats.",
            remedy="Inspect debug/ and update parsers.py",
        ))

    return Envelope(
        command="selfcheck",
        ok=True,
        data={"url": ref.url, "total": venue.total, "unique": venue.unique,
              "monthly": venue.monthly, "coords_embedded": venue.has_coords,
              "search": search_shape},
        next_actions=["python -m beer_in_this_town run --no-upload --json"],
    )


# Asked for, and not possible. Shared by `closures` and `run --check-closed`
# so the two cannot answer differently about the same missing key.
#
# The stage no-ops when nobody asked for it -- that is #20's rule. But a
# command invoked explicitly, or a flag passed deliberately, is somebody
# asking, and quietly doing nothing in reply is how a user concludes that
# every venue is open.
_NO_KEY = Problem(
    code="places_key_missing",
    message="GOOGLE_PLACES_KEY is not set, so no closure check can run.",
    remedy="Set GOOGLE_PLACES_KEY to a key with the Places API enabled and "
           "billing on, then re-run. It is deliberately separate from "
           "GOOGLE_GEOCODING_KEY: different SKU, and sending addresses to "
           "Places is a choice worth making on its own.",
)

_PLACES_REMEDY = (
    "Check GOOGLE_PLACES_KEY, that the Places API (New) is enabled on that "
    "project, and that billing is on. This is not per-venue -- nothing was "
    "written."
)


def cmd_closures(s: Settings, csv_path: str, out: str | None,
                 limit: int | None) -> Envelope:
    """Ask Google Places whether the venues in a CSV still trade.

    Read-only as far as the user's account goes: this touches Places, never
    Maps. It writes a new CSV rather than editing the one it was given, so a
    corpus that took a hundred requests to build is never overwritten by a
    stage that costs money and can fail halfway.
    """
    source = Path(csv_path)
    if not source.exists():
        return fail("closures", Problem(
            code="csv_missing",
            message=f"No such CSV: {source}",
            remedy="Run `run` first, or pass --csv with a path that exists.",
        ))

    if not s.google_places_key:
        return fail("closures", _NO_KEY)

    venues = venues_from_csv(source)
    if not venues:
        return fail("closures", Problem(
            code="csv_missing",
            message=f"{source.name} carried no venue rows.",
            remedy="Check the file is one this project wrote, then re-run.",
        ))

    # A trial before the bulk, for the same reason `pin` has one: this is the
    # billed path, and finding out the query shape is wrong on venue 3 costs
    # less than finding out on venue 300.
    checked = venues[:limit] if limit else venues
    try:
        resolved = resolve_closures(checked, s)
    except PlacesUnavailable as exc:
        return fail("closures", Problem(
            code="places_unavailable",
            message=str(exc),
            remedy=_PLACES_REMEDY,
        ), venues_read=len(venues))

    # The rows not checked keep whatever status they already carried, so a
    # --limit run produces a complete CSV rather than a truncated one.
    merged = resolved + venues[len(checked):]
    target = Path(out) if out else DATA_DIR / f"checked_{source.stem}.csv"
    write_csv(merged, target)

    closed = [v for v in merged if v.is_closed]
    warnings = []
    if limit and limit < len(venues):
        warnings.append(
            f"Trial run: {len(checked)} of {len(venues)} venues were checked. "
            f"The rest were copied through unchanged."
        )
    unmatched = [v for v in resolved if v.business_status == "unmatched"]
    if unmatched:
        warnings.append(
            f"{len(unmatched)} venue(s) had no Places match and are recorded "
            f"as unmatched, NOT as closed. A failed lookup means the place "
            f"closed, was renamed, is too new, or Places simply lacks it -- "
            f"and those want opposite outcomes, so none is chosen."
        )

    return Envelope(
        command="closures",
        ok=True,
        data={
            "csv": str(target),
            "venues": len(merged),
            "checked": len(checked),
            "closed": len(closed),
            "by_status": closure_counts(merged),
        },
        warnings=warnings,
        next_actions=[],
        hints=[
            f'Closed venues are flagged in "{target}", not removed. Nothing '
            f'downstream drops them on its own: deciding what to do with a '
            f'venue Google calls shut is yours.',
            *([f"Flagged closed: {', '.join(v.ref.name for v in closed[:5])}"
               + (f" (+{len(closed) - 5} more)" if len(closed) > 5 else "")]
              if closed else []),
        ],
    )


def cmd_ui(s: Settings, port: int, open_browser: bool) -> Envelope:
    """Serve the setup dashboard on localhost until interrupted.

    Blocks. The envelope is printed on the way out, so `--json` describes the
    session that just ended rather than one about to start.
    """
    from .ui import serve

    try:
        url = serve(s, port=port, open_browser=open_browser)
    except OSError as exc:
        return fail("ui", Problem(
            code="port_unavailable",
            message=f"Could not bind port {port}: {exc}",
            remedy="Something is already using it. Pass --port with another "
                   "number, or stop the other dashboard.",
        ))
    return Envelope(
        command="ui", ok=True, data={"url": url},
        hints=["The dashboard connects and tests accounts. It cannot pin or "
               "write notes -- those have no route on that server."],
    )


def cmd_label(s: Settings, csv_path: str, out: str | None,
              quota: int, seed: int) -> Envelope:
    """Emit a labelling sheet: a stratified sample for a human to judge.

    Reads only what `run` already wrote. No network, no browser, no account.
    """
    source = Path(csv_path)
    if not source.exists():
        return fail("label", Problem(
            code="csv_missing",
            message=f"No such CSV: {source}",
            remedy="Run `run` first, or pass --csv with a path that exists.",
        ))

    venues = venues_from_csv(source)
    if not venues:
        return fail("label", Problem(
            code="csv_missing",
            message=f"{source.name} carried no venue rows.",
            remedy="Check the file is one this project wrote, then re-run.",
        ))

    sheet = stratified_sample(venues, quota=quota, seed=seed)
    target = Path(out) if out else DATA_DIR / f"labels_{source.stem}.csv"
    write_sheet(sheet, target)

    warnings = []
    # An early `run` -- and the seed CSV -- carried no category column at all.
    # Every venue is then "unsettled" by definition, so a labeller would spend
    # 45 minutes answering true_kind against a classifier that never had an
    # opinion to be wrong about. Say so before they start, not after.
    if not any(v.ref.category for v in venues):
        warnings.append(
            "No venue in this CSV has a category, so every kind prediction is "
            "'unsettled' and the venue-kind measurement will say nothing. The "
            "closure and private-space measurements are unaffected. Re-scrape "
            "with a current `run` to measure kind."
        )
    thin = [b for b, n in sheet.counts_by_stratum().items() if n < quota]
    if thin:
        warnings.append(
            f"{len(thin)} bucket(s) held fewer venues than the quota and were "
            f"taken whole: {', '.join(sorted(thin))}"
        )

    return Envelope(
        command="label",
        ok=True,
        data={
            "sheet": str(target),
            "rows": len(sheet.rows),
            "venues": len(venues),
            "buckets": sheet.counts_by_stratum(),
            "bucket_population": sheet.stratum_sizes,
        },
        warnings=warnings,
        next_actions=[
            f'python -m beer_in_this_town score --labels "{target}" --json',
        ],
        hints=[
            f'Fill in is_public, is_open and true_kind in "{target}" first.',
            "Answer every row: picking which ones to fill in breaks the "
            "weighting. y / n / ? are the accepted answers, and ? is a real "
            "one -- it is recorded as an abstention rather than guessed into "
            "a verdict.",
            "A model must not fill these in. The point is a human judgement "
            "to check the classifier against; a model labelling its own "
            "classifier's output measures nothing.",
        ],
    )


def cmd_score(s: Settings, labels_path: str) -> Envelope:
    """Report how wrong the candidate classifier is, by direction."""
    source = Path(labels_path)
    if not source.exists():
        return fail("score", Problem(
            code="csv_missing",
            message=f"No such labelling sheet: {source}",
            remedy="Generate one first: python -m beer_in_this_town label "
                   "--csv data/venues_<city>_<date>.csv --json",
        ))

    try:
        rows, sizes = read_sheet(source)
    except ValueError as exc:
        return fail("score", Problem(
            code="labels_unusable",
            message=str(exc),
            remedy="Re-generate the sheet with `label` and copy your answers "
                   "into it.",
        ))

    try:
        report = score_labels(rows, sizes)
    except LabelsUnusable as exc:
        # A value outside the vocabulary. Guessing at it is how a typo becomes
        # a verdict, so the row and the cell are named instead.
        return fail("score", Problem(
            code="labels_unusable",
            message=str(exc),
            remedy="Fix that cell and re-run. Answers are y, n or ? (or yes / "
                   "no); true_kind must be one of the kinds `label` predicts.",
        ))
    except PartialStratum as exc:
        return fail("score", Problem(
            code="labels_incomplete",
            message=str(exc),
            remedy="Label at least a few rows in every bucket. The rare "
                   "buckets are the ones the measurement exists for.",
        ))

    # The rows behind each rate, grouped, so the failures can be read rather
    # than counted. A rate says how bad; only the rows say why.
    disagreements = report.pop("disagreements")
    dump = DATA_DIR / f"disagreements_{source.stem}.json"
    dump.write_text(json.dumps(disagreements, indent=1), encoding="utf-8")

    return Envelope(
        command="score",
        ok=True,
        data={**report, "disagreements": str(dump),
              "disagreement_counts": {k: len(v) for k, v in disagreements.items()}},
        warnings=report.get("warnings", []),
        next_actions=[],
        hints=[
            f"Read {dump} before changing any threshold: a rate says how bad, "
            f"only the rows say why.",
            "The thresholds live in beer_in_this_town/classify.py.",
        ],
    )


def cmd_run(s: Settings, *, upload: bool, force_browser: bool,
            skip_robots: bool, formats: tuple[str, ...] = ("kml",),
            check_closed: bool = False) -> Envelope:
    ensure_dirs()
    stamp = today_stamp()

    # Before a single request. A run that scrapes a hundred venues and only
    # then discovers it cannot do the thing it was asked to do has wasted the
    # expensive part -- and the expensive part is the one with an account
    # attached.
    if check_closed and not s.google_places_key:
        return fail("run", _NO_KEY)

    with PoliteClient(s) as client:
        if s.respect_robots and not skip_robots and client.robots_disallows_scraping():
            return fail("run", Problem(
                code="robots_disallow",
                message="untappd.com/robots.txt disallows /v/ or /search for *.",
                remedy="Pass --i-read-robots to override, accepting that it is "
                       "against the site's stated wishes and its ToS.",
            ))

        try:
            refs = collect_venue_refs(client, s, force_browser=force_browser)
        except SearchLoginRequired as exc:
            return fail("run", Problem(
                code="search_login_required",
                message=str(exc),
                remedy=f"Sign in to Untappd once in the browser profile at "
                       f"{s.profile_dir}; the search path reuses it. Note that "
                       f"bootstrap only detects a Google session and will "
                       f"report login_not_detected for an Untappd-only login.",
            ))
        except ParseError as exc:
            # Both search paths failed to parse. Say which gate caught it, so
            # this does not surface as a bare unexpected_error.
            return fail("run", Problem(
                code="selectors_stale",
                message=str(exc),
                remedy="Neither the HTTP nor the browser search page parsed. "
                       "Inspect debug/*.html and update the search selectors "
                       "in beer_in_this_town/parsers.py, then re-run.",
            ))
        log.info("Collected %d venue references", len(refs))

        def progress(i: int, n: int, ref: VenueRef) -> None:
            log.info("[%3d/%d] %s", i, n, ref.name)

        venues = fetch_venues(client, refs, progress=progress)

    # Gate: abort before writing anything if the parse looks degraded.
    try:
        assert_corpus_quality(venues, s.parse_strictness)
    except ParseError as exc:
        return fail("run", Problem(
            code="corpus_quality_gate",
            message=str(exc),
            remedy="Inspect debug/*.html and update parsers.py, then re-run. "
                   "Nothing was written -- this is the gate working.",
        ), venues_scraped=len(venues))

    try:
        venues = geocode_missing(venues, s)
    except GeocoderUnavailable as exc:
        # Nothing has been written yet, same as the corpus gate above. A KML
        # missing most of its pins because a key was rejected is worse than no
        # KML at all, because it looks like a finished run.
        return fail("run", Problem(
            code="geocoder_unavailable",
            message=str(exc),
            remedy="Check GOOGLE_GEOCODING_KEY and that billing is enabled on "
                   "it, or unset it to fall back to Nominatim. If no key is "
                   "set, check connectivity -- Nominatim may be throttling or "
                   "blocking this client. Nothing was written.",
        ), venues_scraped=len(venues))

    if check_closed:
        try:
            venues = resolve_closures(venues, s)
        except PlacesUnavailable as exc:
            # Same posture as the corpus gate and the geocoder above: nothing
            # has been written, and a corpus half-annotated by a key that died
            # partway looks finished while being partly unasked.
            return fail("run", Problem(
                code="places_unavailable",
                message=str(exc),
                remedy=_PLACES_REMEDY,
            ), venues_scraped=len(venues))

    csv_path = write_csv(venues, DATA_DIR / f"venues_{s.query}_{stamp}.csv")

    base = DATA_DIR / f"venues_{s.query}_{stamp}"
    # KML stays in the default set so existing workflows are untouched.
    written: dict[str, str] = {}
    if "kml" in formats:
        written["kml"] = str(write_kml(venues, base.with_suffix(".kml"), s.map_title))
    if "geojson" in formats:
        written["geojson"] = str(write_geojson(venues, base.with_suffix(".geojson")))
    if "gpx" in formats:
        written["gpx"] = str(write_gpx(venues, base.with_suffix(".gpx"), s.map_title))
    kml_path = Path(written["kml"]) if "kml" in written else None

    diff = diff_against_previous(venues, s.query)
    write_diff_outputs(diff, stamp)
    commit_run(venues, s.query)
    # So `status` answers about this city, not about Settings() defaults.
    record_run(query=s.query, map_title=s.map_title, csv_path=csv_path)

    warnings = []
    if len(venues) < s.target_count:
        warnings.append(
            f"Asked for {s.target_count} venues and got {len(venues)}. Either "
            f"the query has no more, or search paging stopped early -- check "
            f"the log for where it stopped before trusting the totals."
        )
    closed = [v for v in venues if v.is_closed]
    if closed:
        warnings.append(
            f"{len(closed)} venue(s) are flagged closed by Google Places and "
            f"are still exported and still in the KML: "
            f"{', '.join(v.ref.name for v in closed[:5])}. Nothing drops them "
            f"automatically -- read the business_status column and decide."
        )
    without_coords = [v.ref.name for v in venues if not v.has_coords]
    if without_coords:
        warnings.append(
            f"{len(without_coords)} venue(s) have no coordinates and are not "
            f"pinned in the KML: {', '.join(without_coords[:5])}"
        )

    map_url = None
    if upload and kml_path is None:
        warnings.append(
            "--upload needs a KML; add kml to --format. Nothing was uploaded."
        )
    elif upload:
        map_url = upload_kml(kml_path, s)
        if not map_url:
            warnings.append("My Maps automation failed; import the KML by hand.")
            print(manual_instructions(kml_path, s.map_title), file=sys.stderr)

    return Envelope(
        command="run",
        ok=True,
        data={
            "venues": len(venues),
            "csv": str(csv_path),
            "kml": written.get("kml"),
            "maps": written,
            "closed": len(closed),
            "by_status": closure_counts(venues) if check_closed else None,
            "new_since_last_run": len(diff["new"]),
            "changed": len(diff["changed"]),
            "my_maps_url": map_url,
        },
        warnings=warnings,
        next_actions=[],
        hints=[
            f'The data is in "{csv_path}". To put it on a Google Maps saved '
            f'list a human can run: pin --csv "{csv_path}" --list '
            f'"{s.map_title}" --limit 3 --json. That writes to the account, so '
            f'it is never started unasked; the map files above need nothing.'
        ],
    )


def resolve_region(region: str | None, path: Path) -> tuple[str | None, list[str]]:
    """Work out the region guard, and say so when it cannot be established.

    None means "not specified" -- read it from the data. An explicit "" means
    the caller switched the guard off deliberately, which is theirs to do.
    """
    if region is not None:
        return (region or None), []
    found = region_from_csv(path)
    if found:
        log.info("Region %r read from the CSV.", found)
        return found, []
    return None, [
        "No city column in this CSV, so no region is appended to the Maps "
        "lookups. A bare venue name can match a place in another country; "
        "pass --region to restore that guard."
    ]


def cmd_pin(s: Settings, csv_path: str, list_name: str, limit: int | None,
            region: str | None, min_gap: float, max_gap: float) -> Envelope:
    """Save places from a CSV into a real Google Maps saved list."""
    path = Path(csv_path)
    if not path.exists():
        return fail("pin", Problem(
            code="csv_missing",
            message=f"CSV not found: {path}",
            remedy="python -m beer_in_this_town run --no-upload --json",
        ))

    places = places_from_csv(path)
    region, region_warnings = resolve_region(region, path)
    log.info("Read %d place(s) from %s", len(places), path)

    try:
        journal = pin_places(places, s, list_name, limit=limit, region=region,
                             min_gap_s=min_gap, max_gap_s=max_gap)
    except AlreadyRunning as exc:
        # Distinct from a cool-off: nothing has to elapse, another process is
        # simply holding the ledger. Reusing guardrail_tripped told the caller
        # to wait out a cool-off that does not exist, while `status` -- which
        # knows nothing about the lock -- reported all clear. An agent
        # following the envelope loop would spin on that.
        return fail("pin", Problem(
            code="already_running",
            message=str(exc),
            remedy="Another run holds the write budget. Wait for it to finish, "
                   "then re-run this exact command; progress is journalled. Do "
                   "not delete the lock unless you are certain nothing is "
                   "running.",
        ))
    except Tripped as exc:
        # A guardrail fired on purpose. This MUST NOT look like an ordinary
        # error: the remedy is to wait, never to retry. Previously this was
        # string-matched into 'list_missing', whose remedy told the caller to
        # re-run -- the opposite of what a trip means.
        return fail("pin", Problem(
            code="guardrail_tripped",
            message=str(exc),
            remedy="Wait. Do not re-run until the cool-off expires; check "
                   "`python -m beer_in_this_town status --json`. Do not delete "
                   "state/rate_ledger.json.",
        ))
    except AmbiguousList as exc:
        # Nothing was saved. Resolving this by picking the nearest name is the
        # wrong-list failure the verification step cannot see afterwards.
        return fail("pin", Problem(
            code="list_ambiguous",
            message=str(exc),
            remedy="Pass --list with the list's exact name, or rename the "
                   "lists in Google Maps so the target is unambiguous.",
        ))
    except RuntimeError as exc:
        text = str(exc)
        code = "not_signed_in" if "Not signed in" in text else "list_missing"
        remedy = ("python -m beer_in_this_town bootstrap" if code == "not_signed_in"
                  else f"Create the list {list_name!r} by hand in Google Maps "
                       "(Saved -> New list), then re-run.")
        return fail("pin", Problem(code=code, message=text, remedy=remedy))
    except Exception as exc:
        return fail("pin", Problem(
            code="pin_failed",
            message=str(exc),
            remedy="Re-run the same command; progress is journalled and resumes.",
        ))

    failed = [k for k, v in journal.items() if v == "failed"]
    missing = [k for k, v in journal.items() if v == "not-found"]
    ambiguous = [k for k, v in journal.items() if v == "ambiguous"]
    saved = [k for k, v in journal.items() if v == "ok"]

    actions = []
    if failed:
        # Keep --limit on the retry. Dropping it turned "retry the three that
        # failed in your trial run" into the bulk run AGENTS.md rule 3
        # forbids, and a human reading a hint is the one who decides to widen
        # it.
        scope = f" --limit {limit}" if limit else ""
        actions.append(
            f'A human can retry the {len(failed)} that failed: pin --csv '
            f'"{path}" --list "{list_name}"{scope} --json'
        )

    return Envelope(
        command="pin",
        ok=not failed,
        data={"saved": len(saved), "failed": len(failed), "not_found": len(missing),
              "ambiguous": len(ambiguous), "failed_names": failed[:20],
              "not_found_names": missing[:20],
              "ambiguous_names": ambiguous[:20], "list": list_name},
        warnings=(
            region_warnings
            + ([f"{len(missing)} place(s) had no Google Maps match"] if missing else [])
            + ([f"{len(ambiguous)} place(s) resolved to a different venue "
                "and were skipped -- check them by hand"] if ambiguous else [])
        ),
        next_actions=[],
        hints=actions,
    )


# --------------------------------------------------------------------------
def cmd_notes(s: Settings, csv_path: str, list_name: str, limit: int | None,
              region: str | None, min_gap: float, max_gap: float) -> Envelope:
    """Write the Untappd stats into each saved place's note field."""
    path = Path(csv_path)
    if not path.exists():
        return fail("notes", Problem(
            code="csv_missing",
            message=f"CSV not found: {path}",
            remedy="python -m beer_in_this_town run --no-upload --json",
        ))

    places = notes_from_csv(path)
    region, region_warnings = resolve_region(region, path)
    log.info("Read %d place(s) with stats from %s", len(places), path)

    try:
        journal = add_notes(places, s, list_name, limit=limit, region=region,
                            min_gap_s=min_gap, max_gap_s=max_gap)
    except AlreadyRunning as exc:
        # Distinct from a cool-off: nothing has to elapse, another process is
        # simply holding the ledger. Reusing guardrail_tripped told the caller
        # to wait out a cool-off that does not exist, while `status` -- which
        # knows nothing about the lock -- reported all clear. An agent
        # following the envelope loop would spin on that.
        return fail("notes", Problem(
            code="already_running",
            message=str(exc),
            remedy="Another run holds the write budget. Wait for it to finish, "
                   "then re-run this exact command; progress is journalled. Do "
                   "not delete the lock unless you are certain nothing is "
                   "running.",
        ))
    except Tripped as exc:
        return fail("notes", Problem(
            code="guardrail_tripped",
            message=str(exc),
            remedy="Wait for the cool-off. Do not retry or delete the ledger.",
        ))
    except AmbiguousList as exc:
        return fail("notes", Problem(
            code="list_ambiguous",
            message=str(exc),
            remedy="Pass --list with the list's exact name, or rename the "
                   "lists in Google Maps so the target is unambiguous.",
        ))
    except RuntimeError as exc:
        # Same split `pin` makes. Without it, a signed-out profile and a
        # missing list both arrived as notes_failed with a "re-run" remedy,
        # which is the one thing that cannot help either.
        message = str(exc)
        if "Not signed in" in message:
            return fail("notes", Problem(
                code="not_signed_in",
                message=message,
                remedy="Run `python -m beer_in_this_town bootstrap` yourself "
                       "and sign in; an agent cannot do this.",
            ))
        if "not found in this account" in message:
            return fail("notes", Problem(
                code="list_missing",
                message=message,
                remedy="Create the list by hand in Google Maps, or pass a "
                       "--list that exists.",
            ))
        return fail("notes", Problem(
            code="notes_failed",
            message=message,
            remedy="Re-run; progress is journalled so completed notes are "
                   "skipped.",
        ))
    except Exception as exc:
        return fail("notes", Problem(
            code="notes_failed",
            message=str(exc),
            remedy="Re-run the same command; progress is journalled.",
        ))

    tally = {k: sum(1 for v in journal.values() if v == k)
             for k in ("ok", "failed", "not-found", "ambiguous", "not-in-list")}
    unpinned = [k for k, v in journal.items() if v == "not-in-list"]
    return Envelope(
        command="notes",
        ok=tally["failed"] == 0,
        data={"written": tally["ok"], "failed": tally["failed"],
              "not_found": tally["not-found"], "ambiguous": tally["ambiguous"],
              "not_in_list": tally["not-in-list"], "list": list_name},
        warnings=(region_warnings
                  + ([f"{len(unpinned)} place(s) are not in the list yet; "
                      "run pin first"] if unpinned else [])),
        next_actions=[],
        hints=([f'A human can retry the {tally["failed"]} that failed: notes '
                f'--csv "{path}" --list "{list_name}" --json']
               if tally["failed"] else []),
    )


def build_parser() -> argparse.ArgumentParser:
    # Shared flags live on a parent parser so they work in BOTH positions:
    # `beer_in_this_town --json status` and `beer_in_this_town status --json`. An agent
    # should not have to remember which side of the subcommand a flag goes on.
    # default=SUPPRESS is load-bearing: with a normal default the SUBparser
    # writes its own False over a True set before the subcommand, so
    # `--json status` would silently print human text instead of JSON.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS,
                        help="emit one machine-readable envelope on stdout "
                             "(logs go to stderr)")

    p = EnvelopeParser(prog="beer_in_this_town", parents=[common])
    # parser_class so a rejected flag on a SUBcommand also emits an
    # envelope -- that is where the pacing floors live.
    sub = p.add_subparsers(dest="cmd", required=True,
                           parser_class=EnvelopeParser)

    sub.add_parser("status", parents=[common],
                   help="where the pipeline is up to, and what to run next")
    sub.add_parser("doctor", parents=[common],
                   help="check preconditions (deps, session, geocoder)")
    boot = sub.add_parser("bootstrap", parents=[common],
                          help="one-time interactive login")
    boot.add_argument("--capture", action="store_true",
                      help="skip the login window and capture the session "
                           "from the existing profile (Chrome must be closed)")
    boot.add_argument("--timeout", type=float, default=900.0,
                      help="seconds to wait for you to close Chrome "
                           "(default 900)")

    check = sub.add_parser("selfcheck", parents=[common],
                           help="verify selectors still work (1 request)")
    check.add_argument("--slug", default="american-taproom-waterloo")
    check.add_argument("--id", dest="venue_id", default="7480946")
    check.add_argument("--skip-search", action="store_true",
                       help="only check the venue page (one request). The "
                            "search probe is what catches a search outage.")

    run = sub.add_parser("run", parents=[common],
                         help="scrape, export, diff, and optionally upload")
    run.add_argument("--query", default="singapore")
    run.add_argument("--count", type=int, default=100)
    run.add_argument("--title", default=None, help='My Maps title, e.g. "Singapore Bars"')
    run.add_argument("--format", default="kml",
                     help="comma-separated map formats: kml (My Maps), geojson "
                          "and gpx (Organic Maps, OsmAnd -- these pin on the "
                          "everyday map). Default: kml")
    run.add_argument("--no-upload", action="store_true", help="write files only")
    run.add_argument("--browser-search", action="store_true",
                     help="force the Show More click path instead of HTTP pagination")
    run.add_argument("--check-closed", action="store_true",
                     help="ask Google Places whether each venue still trades "
                          "and record it in a business_status column. Needs "
                          "GOOGLE_PLACES_KEY. Flags; never drops.")
    run.add_argument("--i-read-robots", action="store_true",
                     help="proceed even if robots.txt disallows these paths")
    run.add_argument("--delay", type=at_least(Settings().min_delay_s, "--delay"),
                     default=None,
                     help="raise the minimum inter-request delay, in seconds. "
                          "It cannot be lowered: the pacing is what keeps this "
                          "from looking like a script.")

    pin = sub.add_parser(
        "pin",
        parents=[common],
        help="save places from a CSV into a real Google Maps saved list "
             "(pins on the everyday map, not a My Maps layer)",
    )
    pin.add_argument("--csv", required=True, help="any CSV this project writes")
    pin.add_argument("--list", dest="list_name", default="Singapore Bars",
                     help="exact name of the existing Google Maps list")
    pin.add_argument("--limit", type=int, default=None,
                     help="only do the first N (use for a small trial run)")
    pin.add_argument("--min-gap", type=at_least(PIN_MIN_GAP, "--min-gap"),
                     default=PIN_MIN_GAP,
                     help=f"minimum seconds between places "
                          f"(default {PIN_MIN_GAP:.0f})")
    pin.add_argument("--max-gap", type=at_least(PIN_MAX_GAP, "--max-gap"),
                     default=PIN_MAX_GAP,
                     help=f"maximum seconds between places "
                          f"(default {PIN_MAX_GAP:.0f})")
    notes = sub.add_parser(
        "notes",
        parents=[common],
        help="write Untappd stats into the note on each saved place",
    )
    notes.add_argument("--csv", required=True)
    notes.add_argument("--list", dest="list_name", default="Singapore Bars")
    notes.add_argument("--limit", type=int, default=None)
    notes.add_argument("--min-gap", type=at_least(NOTES_MIN_GAP, "--min-gap"),
                       default=NOTES_MIN_GAP)
    notes.add_argument("--max-gap", type=at_least(NOTES_MAX_GAP, "--max-gap"),
                       default=NOTES_MAX_GAP)
    notes.add_argument("--region", default=None,
                       help="as for pin; read from the CSV when not given")

    label = sub.add_parser(
        "label",
        parents=[common],
        help="emit a stratified sample to label by hand, so the venue "
             "heuristics can be measured rather than guessed at",
    )
    label.add_argument("--csv", required=True, help="any CSV this project wrote")
    label.add_argument("--out", default=None, help="where to write the sheet")
    label.add_argument("--quota", type=int, default=DEFAULT_QUOTA,
                       help=f"rows per bucket (default {DEFAULT_QUOTA}). Every "
                            f"bucket gets the same quota, rare ones included; "
                            f"scoring divides that back out.")
    label.add_argument("--seed", type=int, default=0,
                       help="sampling seed; the same seed regenerates the same "
                            "sheet")

    sub.add_parser("verify", parents=[common],
                   help="test that both accounts actually work (headless, "
                        "read-only, no browser window)")

    ui = sub.add_parser(
        "ui",
        parents=[common],
        help="open the setup dashboard: connect and test your Google and "
             "Untappd accounts, and watch what the tool is doing",
    )
    ui.add_argument("--port", type=int, default=UI_DEFAULT_PORT,
                    help=f"localhost port (default {UI_DEFAULT_PORT}). Bound "
                         f"to 127.0.0.1 only.")
    ui.add_argument("--no-open", action="store_true",
                    help="do not open a browser; just print the URL")

    closures = sub.add_parser(
        "closures",
        parents=[common],
        help="ask Google Places whether the venues in a CSV still trade "
             "(flags them; never drops them)",
    )
    closures.add_argument("--csv", required=True, help="any CSV this project wrote")
    closures.add_argument("--out", default=None,
                          help="where to write the annotated CSV. Defaults to "
                               "data/checked_<name>.csv -- the input is never "
                               "overwritten.")
    closures.add_argument("--limit", type=int, default=None,
                          help="only check the first N. This is the billed "
                               "path; trial it before the bulk.")

    score = sub.add_parser(
        "score",
        parents=[common],
        help="report the venue heuristics' error rates from a labelled sheet",
    )
    score.add_argument("--labels", required=True,
                       help="a sheet written by `label`, with answers filled in")

    pin.add_argument("--region", default=None,
                     help="appended to each search so a name cannot match the "
                          "wrong country. Read from the CSV's city column when "
                          "not given; pass '' to disable")
    return p


# Every subcommand name, so a bare invocation can be told from a mistyped one.
_SUBCOMMANDS = frozenset({
    "status", "doctor", "bootstrap", "selfcheck", "run", "pin", "notes",
    "label", "score", "closures", "ui", "verify",
})


def wants_dashboard(raw: list[str], isatty: bool) -> bool:
    """Should `beertown` with no subcommand open the dashboard?

    A first-time user types the program's name. Answering that with an
    argparse error is the worst possible first impression from a tool whose
    whole first-run story is a dashboard that explains itself -- so a bare
    invocation opens it.

    Three guards, because `ui` blocks forever and AGENTS.md tells an agent not
    to run it. Any of them failing falls back to the ordinary envelope error:

      * `--json` asks for the machine contract, and a blocking server is not
        an envelope. An agent calling `beertown --json` must get its error,
        not a hung process.
      * A non-tty means output is piped or captured -- automation, a CI step,
        a subprocess -- none of which can close a browser window.
      * Anything that looks like an actual request (a subcommand, a typo, or
        --help) is still answered as asked. Silently swallowing a mistyped
        subcommand into the dashboard would hide the typo.
    """
    if not isatty:
        return False
    for token in raw:
        if token in ("--json", "-h", "--help"):
            return False
        if not token.startswith("-"):
            return False  # a subcommand, or a typo that deserves its error
    return True


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if wants_dashboard(raw, sys.stdout.isatty()):
        log.info("No command given -- opening the setup dashboard.")
        raw = [*raw, "ui"]
    args = build_parser().parse_args(raw)
    # SUPPRESS means the attribute is absent unless the flag was passed.
    as_json = getattr(args, "json", False)
    verbose = getattr(args, "verbose", False)
    setup_logging(verbose, as_json)
    s = Settings.from_env()

    if args.cmd in {"pin", "notes"}:
        try:
            check_pacing(args.min_gap, args.max_gap)
        except ValueError as exc:
            emit(fail(args.cmd, Problem(
                code="bad_pacing",
                message=str(exc),
                remedy="Pass --min-gap below --max-gap, or omit both and use "
                       "the defaults, which are chosen to look human.",
            )), as_json)
            return 1

    if args.cmd in {"run", "pin", "notes"}:
        s = replace(
            s,
            query=getattr(args, "query", s.query),
            target_count=getattr(args, "count", s.target_count),
            map_title=getattr(args, "title", None) or getattr(
                args, "list_name", s.map_title),
        )
        if getattr(args, "delay", None):
            s = replace(s, min_delay_s=args.delay, max_delay_s=args.delay * 2.0)

    try:
        if args.cmd == "status":
            env = cmd_status(s)
        elif args.cmd == "doctor":
            env = cmd_doctor(s)
        elif args.cmd == "bootstrap":
            env = cmd_bootstrap(s, args.timeout, args.capture)
        elif args.cmd == "label":
            env = cmd_label(s, args.csv, args.out, args.quota, args.seed)
        elif args.cmd == "verify":
            env = cmd_verify(s)
        elif args.cmd == "ui":
            env = cmd_ui(s, args.port, open_browser=not args.no_open)
        elif args.cmd == "closures":
            env = cmd_closures(s, args.csv, args.out, args.limit)
        elif args.cmd == "score":
            env = cmd_score(s, args.labels)
        elif args.cmd == "selfcheck":
            env = cmd_selfcheck(s, args.slug, args.venue_id,
                                probe_search=not args.skip_search)
        elif args.cmd == "run":
            try:
                formats = parse_formats(args.format)
            except ValueError as exc:
                env = fail("run", Problem(
                    code="bad_format",
                    message=str(exc),
                    remedy="Re-run with --format kml (My Maps), geojson or gpx "
                           "(Organic Maps, OsmAnd), comma-separated.",
                ))
                emit(env, as_json)
                return 1
            env = cmd_run(s, upload=not args.no_upload,
                          force_browser=args.browser_search,
                          skip_robots=args.i_read_robots,
                          formats=formats,
                          check_closed=args.check_closed)
        elif args.cmd == "notes":
            env = cmd_notes(s, args.csv, args.list_name, args.limit,
                            # NOT `or None`: "" is the caller switching the
                            # guard off, None is "work it out from the CSV".
                            args.region, args.min_gap, args.max_gap)
        elif args.cmd == "pin":
            env = cmd_pin(s, args.csv, args.list_name, args.limit,
                          args.region, args.min_gap, args.max_gap)
        else:  # pragma: no cover -- argparse enforces the choices
            raise SystemExit(f"unknown command {args.cmd}")
    except KeyboardInterrupt:
        env = fail(args.cmd, Problem(
            code="interrupted",
            message="Interrupted by the user.",
            remedy="Re-run the same command; progress is journalled and resumes.",
        ))
    except (RateLimitTripped, BudgetExceeded) as exc:
        # Deliberate throttle abort. Advising an immediate retry here would
        # walk straight back into the rate limit.
        env = fail(args.cmd, Problem(
            code="rate_limited",
            message=str(exc),
            remedy="Wait several hours before retrying. The disk cache means "
                   "little work is repeated when you do.",
        ))
    except AlreadyRunning as exc:
        env = fail(args.cmd, Problem(
            code="already_running",
            message=str(exc),
            remedy="Another run holds the write budget. Wait for it to finish, "
                   "then re-run; progress is journalled.",
        ))
    except TransportUnavailable as exc:
        env = fail(args.cmd, Problem(
            code="network_unavailable",
            message=str(exc),
            remedy="Check connectivity and re-run. Nothing was written; the "
                   "run stopped rather than sleeping through the backoff "
                   "ladder once per remaining venue.",
        ))
    except Tripped as exc:
        env = fail(args.cmd, Problem(
            code="guardrail_tripped",
            message=str(exc),
            remedy="Wait for the cool-off to expire. Do not retry.",
        ))
    except PlacesUnavailable as exc:
        # Both call sites catch this already. The net is here because
        # `PlacesUnavailable` subclasses RuntimeError and the fall-through
        # below answers "re-run with -v", which is the one thing that cannot
        # help a rejected key -- and #20 adds callers to this module.
        env = fail(args.cmd, Problem(
            code="places_unavailable",
            message=str(exc),
            remedy=_PLACES_REMEDY,
        ))
    except Exception as exc:
        log.error("Run aborted: %s", exc, exc_info=verbose)
        env = fail(args.cmd, Problem(
            code="unexpected_error",
            message=f"{type(exc).__name__}: {exc}",
            remedy="Re-run with -v for a traceback.",
        ))

    emit(env, as_json)
    return 0 if env.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
