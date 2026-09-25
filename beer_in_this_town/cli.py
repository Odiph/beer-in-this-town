"""Command-line entry point, designed to be driven by a coding agent.

Every command accepts --json and then prints exactly one envelope on stdout
(see agent_io.py). Logs go to stderr. Failures still print a valid envelope
carrying a machine-readable error code and a remedy, so an agent can recover
without parsing tracebacks.

  python -m beer_in_this_town status --json      # where am I, what is next
  python -m beer_in_this_town doctor --json      # are the preconditions met
  python -m beer_in_this_town bootstrap          # one-time interactive login
  python -m beer_in_this_town selfcheck --json   # 1 request: are selectors alive
  python -m beer_in_this_town sweep --city "Tel Aviv" --json   # 1_sweep.csv
  python -m beer_in_this_town enrich|filter|export --city ... --json
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

from . import consent, flow_cmds
from .adb_device import AdbDevice, AdbUnavailable
from .agent_io import Envelope, Problem, emit, fail, log_to_stderr
from .app_calibrate import CalibrationFailed
from .app_categories import CategoryPanelError
from .app_map import WrongScreen
from .app_pipeline import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MIN_DEPTH,
    CityNotFound,
    NoLocationControl,
    census,
    census_envelope,
    write_census,
)
from .app_sweep import DeadPan
from .config import (
    DATA_DIR,
    DEFAULT_METHOD,
    SWEEP_METHODS,
    Settings,
    cli_arg,
    ensure_dirs,
)
from .emulator_checks import check_emulator, emulator_ready, first_failure
from .export import write_csv
from .geocode import GeocoderUnavailable
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
from .notes import MAX_GAP_S as NOTES_MAX_GAP
from .notes import MIN_GAP_S as NOTES_MIN_GAP
from .notes import add_notes, notes_from_csv
from .overpass import OverpassUnavailable
from .parsers import ParseError, StatsLoginRequired, parse_venue_stats
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
from .search_sweep import DEFAULT_TOP, SEARCH_CAP, cmd_search_sweep
from .state import (
    blocked_on,
    hints,
    inspect_state,
    next_actions,
    record_run,
    record_verification,
    sweep_method,
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


def consent_days(raw: str) -> int:
    """--days for allow-writes: a whole number of days, 1 to MAX_DAYS."""
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number "
                                         f"of days.") from exc
    if not 1 <= value <= consent.MAX_DAYS:
        raise argparse.ArgumentTypeError(
            f"--days must be between 1 and {consent.MAX_DAYS}. Consent is "
            f"meant to be given again, not left standing.")
    return value


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
    # The emulator is probed only when a sweep is the next stage; see
    # inspect_state. Each adb call has a short timeout, and a machine with no
    # adb at all answers at once with a failed check, not an exception.
    state = inspect_state(s, probe_emulator=True, emulator=check_emulator)
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
    unran_now = [k for k, v in results.items() if not v["ran"]]
    # Record only a real verdict. A probe that could not run has established
    # nothing, and writing it would let "the browser is broken" masquerade as
    # "the accounts are signed out" for the next twelve hours.
    if not unran_now:
        record_verification(results, ok=not failed)
    # A probe that could not run is not a signed-out account, and the two
    # want opposite remedies. Never collapse them -- see VerifyResult.ran.
    unran = unran_now

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
                   "a password, so an agent cannot do it. Google holds the "
                   "saved list; Untappd on the web is what `enrich` reads "
                   "venue pages through.",
        ), accounts=results)

    return Envelope(
        command="verify", ok=True,
        data={"accounts": results, "verified": True},
        next_actions=["python -m beer_in_this_town status --json"],
    )


def cmd_doctor(s: Settings, emulator=None) -> Envelope:
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
            "playwright missing (needed for sign-in, enrich, pin and notes) "
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

    # The sweep's half of the setup: BlueStacks, adb, the app, the display.
    # Each failure carries a remedy a stranger can follow, in the order they
    # have to be fixed -- a later check is only meaningful once the earlier
    # ones pass.
    checks = (emulator or check_emulator)(s.adb_serial)
    data["emulator"] = [c.to_dict() for c in checks]
    data["emulator_ready"] = emulator_ready(checks)
    first = first_failure(checks)
    if first is not None:
        problems.append(
            f"emulator not ready ({first.name}): {first.detail} "
            f"Fix: {first.remedy}")
    else:
        notes.append(
            "Before each sweep, open the Untappd app on Discover -> View Map.")

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


def cmd_selfcheck(s: Settings, slug: str, venue_id: str) -> Envelope:
    """Cheap pre-flight: one known-good venue page, its shape asserted.

    Venue pages are what `enrich` reads, so they are the one Untappd surface
    whose markup this tool still depends on. One paced request.
    """
    ref = VenueRef(venue_id=venue_id, slug=slug, name="selfcheck",
                   category=None, address=None, city=None)
    try:
        with PoliteClient(s) as client:
            html = client.get(ref.url, use_cache=False)
            venue = parse_venue_stats(html, ref)
    except StatsLoginRequired as exc:
        # A ParseError by type, but an account problem: sending the user to
        # fix selectors would send them after a debug dump that was never
        # written.
        return fail("selfcheck", Problem(
            code="not_signed_in",
            message=str(exc),
            remedy="Sign in to untappd.com in the tool's Chrome profile: run "
                   "`beertown ui` and use the Accounts step. It needs a "
                   "password, so it is the human's to do.",
        ))
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
              "monthly": venue.monthly, "coords_embedded": venue.has_coords},
        next_actions=["python -m beer_in_this_town status --json"],
    )


# Asked for, and not possible.
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
            remedy="Run the collection stages first (`status --json` says "
                   "which is next), or pass --csv with a path that exists.",
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


def cmd_ui(s: Settings, port: int, open_browser: bool,
           detach: bool = False) -> Envelope:
    """Serve the setup dashboard on localhost.

    `--detach` starts it in its own process and returns immediately, which is
    what makes it something an agent can open for a user. Without it this
    blocks, and the envelope is printed on the way out.
    """
    from .ui import serve, serve_detached

    if detach:
        try:
            rec = serve_detached(s, port=port)
        except (RuntimeError, OSError) as exc:
            # OSError as well: `Popen` raises it when the interpreter cannot
            # be spawned at all, and RuntimeError-only sent that to
            # `unexpected_error`, whose remedy is "re-run with -v" -- advice
            # that cannot help a process that will not start.
            return fail("ui", Problem(
                code="port_unavailable",
                message=str(exc),
                remedy=f"Pass --port with a number other than {port}, or stop "
                       f"whatever is already listening on it.",
            ))
        return Envelope(
            command="ui", ok=True,
            data={"url": rec["url"], "pid": rec["pid"], "port": rec["port"],
                  "started": rec["started"]},
            hints=[
                ("Opened the setup dashboard." if rec["started"]
                 else "A dashboard was already running for this checkout.")
                + f" Send the user to {rec['url']} — it walks them through "
                  f"signing in. The link carries the key for that session.",
                "Signing in needs their password, so it is theirs to do. When "
                "they say they are done, run `verify --json` to check it took.",
            ],
            next_actions=[],
        )

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

    Reads only a CSV the pipeline already wrote. No network, no browser,
    no account.
    """
    source = Path(csv_path)
    if not source.exists():
        return fail("label", Problem(
            code="csv_missing",
            message=f"No such CSV: {source}",
            remedy="Run the collection stages first (`status --json` says "
                   "which is next), or pass --csv with a path that exists.",
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
            "closure and private-space measurements are unaffected. Use an "
            "enriched CSV (data/<city>/2_enriched.csv or later) to measure "
            "kind."
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
                   "--csv data/<city>/3_venues.csv --json",
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


def cmd_sweep(s: Settings, *, here: bool, min_depth: int, max_depth: int,
              formats: tuple[str, ...], device=None,
              emulator=None, fresh: bool = False) -> Envelope:
    """Find a city's venues from the Untappd app's map, on an emulator.

    This is the collection step, and the only one: the app's map is a real
    geographic search, where Untappd's web search matched venue *names*. It
    reads only, resumes from its journal if interrupted, and calibrates the
    map's scale against OpenStreetMap before writing a single coordinate.

    The emulator checks run first. Finding out that adb is missing, the app
    is not installed or the display is the wrong size used to cost a failed
    sweep with a remedy that could only guess at the cause.
    """
    checks = (emulator or check_emulator)(s.adb_serial)
    if not emulator_ready(checks):
        first = first_failure(checks)
        return fail("sweep", Problem(
            code="emulator_unavailable",
            message=f"The emulator is not ready ({first.name}): {first.detail}",
            remedy=f"{first.remedy} Then run `beertown doctor --json`; it "
                   f"re-runs every emulator check. Nothing was swept.",
        ), emulator=[c.to_dict() for c in checks])

    ensure_dirs()
    device = device or AdbDevice(serial=s.adb_serial)
    c = census(device, s.query, s, here=here, min_depth=min_depth,
               max_depth=max_depth, fresh=fresh)
    csv_path, written = write_census(c, s.query, s.map_title, formats)
    record_run(query=s.query, map_title=s.map_title, csv_path=csv_path,
               method="map")
    return census_envelope(c, s.query, s.map_title, csv_path, written)


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


def _no_consent(command: str, list_name: str) -> Envelope | None:
    """The failure envelope when `list_name` has no live consent, else None.

    Checked before the CSV, the pre-flight and any browser launch: without
    consent there is nothing for `pin` or `notes` to do, so nothing is done.
    The remedy is prose, not a command, so `fail()` keeps it out of
    `next_actions` (and `allow-writes` is on its human-only list anyway).
    """
    if consent.has_consent(list_name):
        return None
    return fail(command, Problem(
        code="no_consent",
        message=f"No recorded consent to write to the list {list_name!r}. "
                f"`{command}` writes to the Google account, so it needs the "
                f"account owner's say-so for this exact list.",
        remedy=f"Only a person can give it. In their own terminal, not "
               f"through an agent: python -m beer_in_this_town allow-writes "
               f"--list {cli_arg(list_name)} -- then re-run this command.",
    ), list=list_name)


_CONSENT_EXPLAINER = """\
You are about to allow `pin` and `notes` to write to the Google Maps saved
list {name!r}, for {days} day(s).

What they do:
  * pin   opens Google Maps in a real Chrome window on this tool's profile,
          signed in as you, and saves each venue from a CSV into that list.
  * notes writes the Untappd check-in stats into each saved place's note.

What that crosses:
  * Google's Terms of Service ask you not to use the service through
    automated means. There is no API for saved lists, so these commands
    drive the Maps interface the way you would. Your account, your call:
    see "Using other people's services" in README.md.
  * The writes land in your real account. The guardrails (100 writes a day,
    60 a run, a circuit breaker, CAPTCHA detection, a six-hour cool-off)
    make trouble less likely; they do not make it permitted.

Consent covers this exact list only, lasts {days} day(s), and can be taken
back at any time with:
  python -m beer_in_this_town allow-writes --revoke --list {arg}
"""


def cmd_allow_writes(list_name: str, days: int, revoke: bool, *,
                     as_json: bool, stdin=None) -> Envelope:
    """Record, or revoke, a person's consent for `pin`/`notes` on one list.

    Granting refuses `--json` and a stdin that is not a terminal: an agent
    must not be able to consent on the account owner's behalf, and a piped
    "yes" is exactly that. Revoking needs neither -- taking permission away
    is always safe, whoever does it.
    """
    stdin = sys.stdin if stdin is None else stdin
    if not list_name.strip():
        return fail("allow-writes", Problem(
            code="bad_arguments",
            message="--list is empty.",
            remedy='Pass --list "<exact name>" of the Google Maps list.',
        ))
    if revoke:
        removed = consent.revoke(list_name)
        return Envelope(
            command="allow-writes", ok=True,
            data={"list": list_name, "revoked": removed},
            warnings=([] if removed else
                      [f"There was no consent recorded for {list_name!r}."]),
        )

    try:
        interactive = bool(stdin.isatty())
    except (AttributeError, ValueError, OSError):
        interactive = False
    if as_json or not interactive:
        return fail("allow-writes", Problem(
            code="human_only",
            message="allow-writes records a person's consent, so it only runs "
                    "at an interactive terminal and never with --json.",
            remedy="The account owner runs it themselves, in their own "
                   "terminal. An agent cannot give this consent.",
        ), list=list_name)

    print(_CONSENT_EXPLAINER.format(name=list_name, days=days,
                                    arg=cli_arg(list_name)))
    print(f"To agree, type the list's name exactly ({list_name}) and press "
          f"Enter. Anything else cancels.")
    print("> ", end="", flush=True)
    try:
        typed = stdin.readline()
    except (OSError, ValueError, KeyboardInterrupt):
        typed = ""
    if typed.strip() != list_name:
        return fail("allow-writes", Problem(
            code="consent_not_given",
            message="The name typed did not match, so nothing was recorded.",
            remedy=f"Run it again and type {list_name} exactly, or leave it: "
                   f"pin and notes stay refused.",
        ), list=list_name)

    record = consent.grant(list_name, days)
    return Envelope(
        command="allow-writes", ok=True,
        data={"list": list_name, "days": days,
              "expires": time.strftime("%Y-%m-%d %H:%M",
                                       time.localtime(record["expires_at"]))},
        hints=["Start with a trial: pin ... --limit 3, and check the three "
               "places in Google Maps before the rest."],
    )


def cmd_pin(s: Settings, csv_path: str, list_name: str, limit: int | None,
            region: str | None, min_gap: float, max_gap: float) -> Envelope:
    """Save places from a CSV into a real Google Maps saved list."""
    if (refused := _no_consent("pin", list_name)) is not None:
        return refused
    path = Path(csv_path)
    if not path.exists():
        return fail("pin", Problem(
            code="csv_missing",
            message=f"CSV not found: {path}",
            remedy="python -m beer_in_this_town status --json",
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
    if (refused := _no_consent("notes", list_name)) is not None:
        return refused
    path = Path(csv_path)
    if not path.exists():
        return fail("notes", Problem(
            code="csv_missing",
            message=f"CSV not found: {path}",
            remedy="python -m beer_in_this_town status --json",
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
             for k in ("ok", "failed", "not-found", "ambiguous", "not-in-list",
                       "no-note-box")}
    unpinned = [k for k, v in journal.items() if v == "not-in-list"]
    no_box = tally["no-note-box"]
    return Envelope(
        command="notes",
        ok=tally["failed"] == 0 and no_box == 0,
        data={"written": tally["ok"], "failed": tally["failed"],
              "not_found": tally["not-found"], "ambiguous": tally["ambiguous"],
              "not_in_list": tally["not-in-list"], "no_note_box": no_box,
              "list": list_name},
        warnings=(region_warnings
                  + ([f"{len(unpinned)} place(s) are not in the list yet; "
                      "run pin first"] if unpinned else [])
                  + ([f"{no_box} place(s) showed no note box for "
                      f"{list_name!r}, so nothing was written to them. If "
                      "every place does this, Google Maps changed its note "
                      "editor again."] if no_box else [])),
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
                   help="check preconditions (deps, session, geocoder, "
                        "and the emulator the sweep drives)")
    boot = sub.add_parser("bootstrap", parents=[common],
                          help="one-time interactive login")
    boot.add_argument("--capture", action="store_true",
                      help="skip the login window and capture the session "
                           "from the existing profile (Chrome must be closed)")
    boot.add_argument("--timeout", type=float, default=900.0,
                      help="seconds to wait for you to close Chrome "
                           "(default 900)")

    check = sub.add_parser("selfcheck", parents=[common],
                           help="check a known venue page still parses "
                                "(1 request)")
    check.add_argument("--slug", default="american-taproom-waterloo")
    check.add_argument("--id", dest="venue_id", default="7480946")

    sw = sub.add_parser("sweep", parents=[common],
                        help="find a city's venues (the collection step): "
                             "Untappd's web search by the city's name "
                             "variants, or the Untappd app's map on an "
                             "Android emulator")
    sw.add_argument("--city", dest="query", default=None,
                    help="the city to sweep. No default: without one, and "
                         "without a city named in the dashboard, `sweep` "
                         "refuses rather than choosing for you.")
    sw.add_argument("--title", default=None,
                    help='the Google Maps list name the results are meant for, '
                         'e.g. "London Bars"')
    sw.add_argument("--method", choices=SWEEP_METHODS, default=None,
                    help="search: Untappd's web search for each spelling of "
                         "the city's name, most-checked-in first (no "
                         "emulator). map: the Untappd app's map on an "
                         "emulator. Default: the method of the last sweep "
                         "or of the city chosen in the dashboard, else "
                         f"{DEFAULT_METHOD}.")
    sw.add_argument("--top", type=int, default=DEFAULT_TOP,
                    help="search: how many results to collect per spelling, "
                         f"most-checked-in first (1-{SEARCH_CAP}, default "
                         f"{DEFAULT_TOP}; the site pages no deeper)")
    sw.add_argument("--fresh", action="store_true",
                    help="sweep again even if a complete sweep of this city "
                         "from the last 12h exists (a re-run otherwise only "
                         "re-does the placement step)")
    sw.add_argument("--here", action="store_true",
                    help="centre on the emulator's own GPS fix (tap Reset "
                         "location) instead of searching the city by name")
    sw.add_argument("--min-depth", type=int, default=DEFAULT_MIN_DEPTH,
                    help=f"always split at least this deep "
                         f"(default {DEFAULT_MIN_DEPTH})")
    sw.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH,
                    help=f"never split deeper than this "
                         f"(default {DEFAULT_MAX_DEPTH})")
    sw.add_argument("--format", default=None,
                    help="also write 1_sweep.<fmt> map files: comma-separated "
                         "kml, geojson, gpx. data/<city>/1_sweep.csv is "
                         "always written.")

    # enrich / filter / export: stream A2's module owns them.
    flow_cmds.add_parsers(sub, common)

    pin = sub.add_parser(
        "pin",
        parents=[common],
        help="save places from a CSV into a real Google Maps saved list "
             "(pins on the everyday map, not a My Maps layer)",
    )
    pin.add_argument("--csv", required=True, help="any CSV this project writes")
    pin.add_argument("--list", dest="list_name", default=None,
                     help="exact name of the existing Google Maps list. No "
                          "default: this writes to your account, and a "
                          "guessed list name is a guess about where.")
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
    notes.add_argument("--list", dest="list_name", default=None)
    notes.add_argument("--limit", type=int, default=None)
    notes.add_argument("--min-gap", type=at_least(NOTES_MIN_GAP, "--min-gap"),
                       default=NOTES_MIN_GAP)
    notes.add_argument("--max-gap", type=at_least(NOTES_MAX_GAP, "--max-gap"),
                       default=NOTES_MAX_GAP)
    notes.add_argument("--region", default=None,
                       help="as for pin; read from the CSV when not given")

    allow = sub.add_parser(
        "allow-writes",
        parents=[common],
        help="(a person, at a terminal) consent to pin/notes writing to one "
             "saved list for a few days; --revoke takes it back",
    )
    allow.add_argument("--list", dest="list_name", required=True,
                       help="the exact name of the Google Maps list")
    allow.add_argument("--days", type=consent_days,
                       default=consent.DEFAULT_DAYS,
                       help=f"how long the consent lasts (default "
                            f"{consent.DEFAULT_DAYS}, at most "
                            f"{consent.MAX_DAYS})")
    allow.add_argument("--revoke", action="store_true",
                       help="remove the consent for this list instead")

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
    ui.add_argument("--detach", action="store_true",
                    help="start it in the background and return immediately, "
                         "instead of blocking. This is the form an agent can "
                         "run to open the dashboard for a person.")

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
    "status", "doctor", "bootstrap", "selfcheck", "sweep", "enrich",
    "filter", "export", "pin", "notes", "label", "score", "closures", "ui",
    "verify", "allow-writes",
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

    if args.cmd in {"sweep", "enrich", "filter", "export", "pin", "notes"}:
        # argv, then what the user named in the dashboard, then nothing.
        # There is no built-in default to fall through to any more: a city
        # nobody chose is a scrape of somewhere nobody asked for, and a list
        # name nobody chose is a write to an account.
        remembered = inspect_state(s)["last_run"]
        query = getattr(args, "query", None) or remembered.get("query") or ""
        if args.cmd in {"pin", "notes"}:
            # Only a name the person typed. The dashboard derives "<City>
            # Bars" as a suggestion; falling back to it made `no_list`
            # unreachable and aimed a write at a list nobody named.
            list_name = getattr(args, "list_name", None) or ""
        else:
            list_name = (getattr(args, "title", None)
                         or remembered.get("map_title") or "")
        s = replace(s, query=query, map_title=list_name)
        if args.cmd == "sweep" and not s.query:
            emit(fail(args.cmd, Problem(
                code="no_city",
                message="No city given, and none has been chosen.",
                remedy='Pass --city "<city>", or name one on the last step '
                       "of `beertown ui`. There is deliberately no default: "
                       "choosing a city for someone is choosing what they "
                       "get.",
            )), as_json)
            return 1
        if args.cmd in {"pin", "notes"} and not s.map_title:
            emit(fail(args.cmd, Problem(
                code="no_list",
                message="No saved list named.",
                remedy='Pass --list "<exact name>". There is no default, '
                       "because this writes into a real Google Maps list and "
                       "a guessed name is a guess about where.",
            )), as_json)
            return 1

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
        elif args.cmd == "allow-writes":
            env = cmd_allow_writes(args.list_name, args.days, args.revoke,
                                   as_json=as_json)
        elif args.cmd == "ui":
            env = cmd_ui(s, args.port, open_browser=not args.no_open,
                         detach=args.detach)
        elif args.cmd == "closures":
            env = cmd_closures(s, args.csv, args.out, args.limit)
        elif args.cmd == "score":
            env = cmd_score(s, args.labels)
        elif args.cmd == "selfcheck":
            env = cmd_selfcheck(s, args.slug, args.venue_id)
        elif args.cmd == "sweep":
            try:
                formats = parse_formats(args.format) if args.format else ()
            except ValueError as exc:
                emit(fail("sweep", Problem(
                    code="bad_format", message=str(exc),
                    remedy="Re-run with --format kml, geojson or gpx, "
                           "comma-separated, or omit it for the CSV alone.",
                )), as_json)
                return 1
            if not 0 <= args.min_depth <= args.max_depth:
                emit(fail("sweep", Problem(
                    code="bad_arguments",
                    message=f"--min-depth {args.min_depth} and --max-depth "
                            f"{args.max_depth} are not a valid range.",
                    remedy="Pass 0 <= --min-depth <= --max-depth, or omit "
                           "both for the measured defaults.",
                )), as_json)
                return 1
            method = args.method or sweep_method(s)
            if method == "search":
                if args.here or not 1 <= args.top <= SEARCH_CAP:
                    emit(fail("sweep", Problem(
                        code="bad_arguments",
                        message=("--here needs the app's map (--method map)."
                                 if args.here else
                                 f"--top {args.top} is outside 1-{SEARCH_CAP}."),
                        remedy="Re-run without --here, or with --method map; "
                               f"--top takes 1 to {SEARCH_CAP}.",
                    )), as_json)
                    return 1
                env = cmd_search_sweep(s, s.query, top=args.top,
                                       formats=formats, title=s.map_title)
            else:
                env = cmd_sweep(s, here=args.here, min_depth=args.min_depth,
                                max_depth=args.max_depth, formats=formats,
                                fresh=args.fresh)
        elif args.cmd == "notes":
            env = cmd_notes(s, args.csv, args.list_name, args.limit,
                            # NOT `or None`: "" is the caller switching the
                            # guard off, None is "work it out from the CSV".
                            args.region, args.min_gap, args.max_gap)
        elif args.cmd == "pin":
            env = cmd_pin(s, args.csv, args.list_name, args.limit,
                          args.region, args.min_gap, args.max_gap)
        else:
            flow = flow_cmds.dispatch(args, s)
            if flow is None:  # pragma: no cover -- argparse enforces choices
                raise SystemExit(f"unknown command {args.cmd}")
            env = flow
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
    except OverpassUnavailable as exc:
        env = fail(args.cmd, Problem(
            code="overpass_unavailable",
            message=str(exc),
            remedy="Overpass is volunteer-run and sheds load; wait and "
                   "re-run, or set OVERPASS_URL to a mirror such as "
                   "https://overpass.kumi.systems/api/interpreter. Nothing "
                   "was written.",
        ))
    except CalibrationFailed as exc:
        env = fail(args.cmd, Problem(
            code="calibration_failed",
            message=str(exc),
            remedy="The map's scale could not be measured against "
                   "OpenStreetMap, so no coordinates were written. Nothing "
                   "was written; the sweep journal is kept, so a re-run "
                   "resumes. Try --here with the emulator's location set "
                   "inside the city.",
        ))
    except CityNotFound as exc:
        env = fail(args.cmd, Problem(
            code="city_not_found",
            message=str(exc),
            remedy="Check the spelling, or add the country (\"Paris, "
                   "France\"). Nothing was swept.",
        ))
    except GeocoderUnavailable as exc:
        env = fail(args.cmd, Problem(
            code="geocoder_unavailable",
            message=str(exc),
            remedy="Check GOOGLE_GEOCODING_KEY and billing, or unset it to "
                   "use Nominatim. Nothing was written.",
        ))
    except NoLocationControl as exc:
        env = fail(args.cmd, Problem(
            code="app_screen_unexpected",
            message=str(exc),
            remedy="Open the Untappd app on Discover -> View Map and re-run, "
                   "or drop --here to centre by city name instead.",
        ))
    except AdbUnavailable as exc:
        env = fail(args.cmd, Problem(
            code="adb_unavailable",
            message=str(exc),
            remedy="Check the emulator is running and `adb devices` lists "
                   "it. Nothing was written.",
        ))
    except DeadPan as exc:
        env = fail(args.cmd, Problem(
            code="app_pan_failed",
            message=str(exc),
            remedy="Gestures are not reaching the map. Bring the Untappd map "
                   "to the front, dismiss any dialog, and re-run; progress "
                   "is journalled, so the sweep resumes.",
        ))
    except CategoryPanelError as exc:
        env = fail(args.cmd, Problem(
            code="app_screen_unexpected",
            message=str(exc),
            remedy="The category filter was not open where it was expected. "
                   "Re-run; the sweep re-checks the screen before acting.",
        ))
    except WrongScreen as exc:
        # Last of the app handlers, because it is the most general of them.
        # All five of these subclass RuntimeError, so without these clauses
        # the fall-through below answers "re-run with -v" -- which is wrong
        # advice for every one of them. Same reasoning as PlacesUnavailable
        # above.
        env = fail(args.cmd, Problem(
            code="app_screen_unexpected",
            message=str(exc),
            remedy="The Untappd app was not showing the map. Open it on "
                   "Discover -> View Map and re-run; progress is journalled.",
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
