"""Command-line entry point, designed to be driven by a coding agent.

Every command accepts --json and then prints exactly one envelope on stdout
(see agent_io.py). Logs go to stderr. Failures still print a valid envelope
carrying a machine-readable error code and a remedy, so an agent can recover
without parsing tracebacks.

  python -m beer_in_this_town status --json      # where am I, what is next
  python -m beer_in_this_town doctor --json      # are the preconditions met
  python -m beer_in_this_town bootstrap          # one-time interactive login
  python -m beer_in_this_town selfcheck --json   # 1 request: are selectors alive
  python -m beer_in_this_town run --json         # scrape -> CSV + KML + diff
  python -m beer_in_this_town pin  --json        # save into a Google Maps list
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

from .agent_io import Envelope, Problem, emit, fail, log_to_stderr
from .config import DATA_DIR, Settings, ensure_dirs
from .export import (
    commit_run,
    diff_against_previous,
    today_stamp,
    write_csv,
    write_diff_outputs,
    write_kml,
)
from .geocode import GeocoderUnavailable, geocode_missing
from .guardrails import AlreadyRunning, Tripped
from .http_client import (
    BudgetExceeded,
    PoliteClient,
    RateLimitTripped,
)
from .models import VenueRef
from .mymaps_upload import manual_instructions, upload_kml
from .notes import MAX_GAP_S as NOTES_MAX_GAP
from .notes import MIN_GAP_S as NOTES_MIN_GAP
from .notes import add_notes, notes_from_csv
from .parsers import ParseError, assert_corpus_quality, parse_venue_stats
from .pin_to_list import MAX_GAP_S as PIN_MAX_GAP
from .pin_to_list import MIN_GAP_S as PIN_MIN_GAP
from .pin_to_list import pin_places, places_from_csv
from .scrape import SearchLoginRequired, collect_venue_refs, fetch_venues
from .state import inspect_state, next_actions, record_run

log = logging.getLogger("beer_in_this_town")


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
    return Envelope(
        command="status",
        ok=True,
        data=state,
        next_actions=next_actions(state, s),
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

    data["session_file"] = "present" if s.storage_state.exists() else "missing"
    data["profile_dir"] = "present" if s.profile_dir.exists() else "missing"
    if not s.storage_state.exists():
        problems.append("no saved session -- run: python -m beer_in_this_town bootstrap")

    data["geocoder"] = "google" if s.google_geocoding_key else "nominatim (free, 1 req/s)"

    return Envelope(
        command="doctor",
        ok=not problems,
        data=data,
        warnings=problems,
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
    """Cheap pre-flight: one venue page, full shape assertion."""
    ref = VenueRef(venue_id=venue_id, slug=slug, name="selfcheck",
                   category=None, address=None, city=None)
    try:
        with PoliteClient(s) as client:
            html = client.get(ref.url, use_cache=False)
            venue = parse_venue_stats(html, ref)
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
        next_actions=["python -m beer_in_this_town run --json"],
    )


def cmd_run(s: Settings, *, upload: bool, force_browser: bool,
            skip_robots: bool) -> Envelope:
    ensure_dirs()
    stamp = today_stamp()

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

    csv_path = write_csv(venues, DATA_DIR / f"venues_{s.query}_{stamp}.csv")
    kml_path = write_kml(venues, DATA_DIR / f"venues_{s.query}_{stamp}.kml", s.map_title)

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
    without_coords = [v.ref.name for v in venues if not v.has_coords]
    if without_coords:
        warnings.append(
            f"{len(without_coords)} venue(s) have no coordinates and are not "
            f"pinned in the KML: {', '.join(without_coords[:5])}"
        )

    map_url = None
    if upload:
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
            "kml": str(kml_path),
            "new_since_last_run": len(diff["new"]),
            "changed": len(diff["changed"]),
            "my_maps_url": map_url,
        },
        warnings=warnings,
        next_actions=[
            f'python -m beer_in_this_town pin --csv "{csv_path}" '
            f'--list "{s.map_title}" --limit 3 --json'
        ],
    )


def cmd_pin(s: Settings, csv_path: str, list_name: str, limit: int | None,
            region: str | None, min_gap: float, max_gap: float) -> Envelope:
    """Save places from a CSV into a real Google Maps saved list."""
    path = Path(csv_path)
    if not path.exists():
        return fail("pin", Problem(
            code="csv_missing",
            message=f"CSV not found: {path}",
            remedy="python -m beer_in_this_town run --json",
        ))

    places = places_from_csv(path)
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
        actions.append(
            f'python -m beer_in_this_town pin --csv "{path}" --list "{list_name}" --json'
        )

    return Envelope(
        command="pin",
        ok=not failed,
        data={"saved": len(saved), "failed": len(failed), "not_found": len(missing),
              "ambiguous": len(ambiguous), "failed_names": failed[:20],
              "not_found_names": missing[:20],
              "ambiguous_names": ambiguous[:20], "list": list_name},
        warnings=(
            ([f"{len(missing)} place(s) had no Google Maps match"] if missing else [])
            + ([f"{len(ambiguous)} place(s) resolved to a different venue "
                "and were skipped -- check them by hand"] if ambiguous else [])
        ),
        next_actions=actions,
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
            remedy="python -m beer_in_this_town run --json",
        ))

    places = notes_from_csv(path)
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
        warnings=([f"{len(unpinned)} place(s) are not in the list yet; "
                   "run pin first"] if unpinned else []),
        next_actions=([f'python -m beer_in_this_town notes --csv "{path}" '
                       f'--list "{list_name}" --json']
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

    p = argparse.ArgumentParser(prog="beer_in_this_town", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

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

    run = sub.add_parser("run", parents=[common],
                         help="scrape, export, diff, and optionally upload")
    run.add_argument("--query", default="singapore")
    run.add_argument("--count", type=int, default=100)
    run.add_argument("--title", default=None, help='My Maps title, e.g. "Singapore Bars"')
    run.add_argument("--no-upload", action="store_true", help="write files only")
    run.add_argument("--browser-search", action="store_true",
                     help="force the Show More click path instead of HTTP pagination")
    run.add_argument("--i-read-robots", action="store_true",
                     help="proceed even if robots.txt disallows these paths")
    run.add_argument("--delay", type=float, default=None,
                     help="override the minimum inter-request delay in seconds")

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
    pin.add_argument("--min-gap", type=float, default=PIN_MIN_GAP,
                     help=f"minimum seconds between places "
                          f"(default {PIN_MIN_GAP:.0f})")
    pin.add_argument("--max-gap", type=float, default=PIN_MAX_GAP,
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
    notes.add_argument("--min-gap", type=float, default=NOTES_MIN_GAP)
    notes.add_argument("--max-gap", type=float, default=NOTES_MAX_GAP)
    notes.add_argument("--region", default="Singapore")

    pin.add_argument("--region", default="Singapore",
                     help="appended to each search so a name cannot match the "
                          "wrong country; pass '' to disable")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # SUPPRESS means the attribute is absent unless the flag was passed.
    as_json = getattr(args, "json", False)
    verbose = getattr(args, "verbose", False)
    setup_logging(verbose, as_json)
    s = Settings.from_env()

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
        elif args.cmd == "selfcheck":
            env = cmd_selfcheck(s, args.slug, args.venue_id)
        elif args.cmd == "run":
            env = cmd_run(s, upload=not args.no_upload,
                          force_browser=args.browser_search,
                          skip_robots=args.i_read_robots)
        elif args.cmd == "notes":
            env = cmd_notes(s, args.csv, args.list_name, args.limit,
                            args.region or None, args.min_gap, args.max_gap)
        elif args.cmd == "pin":
            env = cmd_pin(s, args.csv, args.list_name, args.limit,
                          args.region or None, args.min_gap, args.max_gap)
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
    except Tripped as exc:
        env = fail(args.cmd, Problem(
            code="guardrail_tripped",
            message=str(exc),
            remedy="Wait for the cool-off to expire. Do not retry.",
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
