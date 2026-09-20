"""What a first-time user has to get right, and whether they have.

Two tiers, and the distinction between them is the point of this module.

**Detection** is free and offline: is Chrome installed, is there a cookie in
the profile, does `storage_state.json` exist. It runs on every page load and
it is honest about being a sniff -- a cookie is evidence that a login once
happened, not that the account works now. Sessions expire, and a profile that
still carries a `SID` cookie against a Google account that has since signed
out looks identical from the filesystem.

**Verification** is a real round-trip and costs one request or one headless
browser. It is what "connected" actually means, and nothing here reports a
connection as working until a verification has said so. A check that has only
been detected says so in as many words.

That split exists because the alternative is the failure this project keeps
running into from the other direction: `bootstrap` detects a Google session
and reports success, and the first thing the user learns about their Untappd
login is `search_login_required`, a hundred requests into a run.

Nothing in this module writes to an account, and nothing here can start `pin`
or `notes`. The dashboard's whole scope is getting a user connected.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from ..config import Settings

log = logging.getLogger(__name__)

# What a state means to the person reading it, rather than to the code:
#   ok        -- ready, and for an account that means verified by round-trip
#   attention -- the user has something to do, and `fix` says what
#   unknown   -- we genuinely cannot tell, and saying "no" would be a guess
#   off       -- not configured, and that is a valid choice, not a fault
OK, ATTENTION, UNKNOWN, OFF = "ok", "attention", "unknown", "off"


@dataclass(frozen=True)
class Check:
    """One row of the setup panel."""

    key: str
    label: str
    state: str
    detail: str
    fix: str = ""            # what the person does about it, in their words
    action: str = ""         # an action id the dashboard can offer as a button
    verified: bool = False   # proven by a live round-trip, not merely detected
    # Somewhere to actually go. A row that says what is wrong and offers no
    # way to act on it leaves the reader to go and find the page themselves,
    # which is the work this dashboard exists to remove.
    links: tuple[tuple[str, str], ...] = ()
    # The longer answer, behind a disclosure: why this row exists and what
    # goes wrong when it is red. Kept out of `detail` so the panel stays
    # scannable for someone who already knows.
    why: str = ""

    def to_row(self) -> dict:
        return {
            "key": self.key, "label": self.label, "state": self.state,
            "detail": self.detail, "fix": self.fix, "action": self.action,
            "verified": self.verified, "why": self.why,
            "links": [{"label": lbl, "url": url} for lbl, url in self.links],
        }


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of one live round-trip."""

    ok: bool
    detail: str
    # What was actually observed, so a failure can be argued with rather than
    # just believed. An empty string means the probe never got that far.
    evidence: str = ""
    # Did the probe actually complete? A browser that will not start and an
    # account that is signed out are both `ok=False` and want opposite advice
    # — "install Playwright" against "sign in again". Collapsing them is how
    # a user gets sent through a login that was never the problem.
    ran: bool = True


# --------------------------------------------------------------------------
# Tier 1: detection. Offline, free, runs on every page load.
# --------------------------------------------------------------------------
def _chrome_check() -> Check:
    from ..chrome_launch import chrome_major_version, find_chrome

    if find_chrome() is None:
        return Check(
            "chrome", "Chrome", ATTENTION,
            "Not found on this machine.",
            fix="Install Google Chrome. The sign-in has to happen in a real "
                "Chrome window — Google refuses logins in an automated one.",
        )
    major = chrome_major_version()
    return Check("chrome", "Chrome", OK,
                 f"Version {major}" if major else "Installed", verified=True)


def _playwright_check() -> Check:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return Check(
            "playwright", "Browser automation", ATTENTION,
            "Playwright is not installed.",
            fix='Run: pip install -e ".[browser]"',
        )
    return Check("playwright", "Browser automation", OK, "Playwright ready",
                 verified=True)


def _google_check(s: Settings, proven: VerifyResult | None) -> Check:
    from ..chrome_launch import profile_has_google_session

    if proven is not None:
        if proven.ok:
            return Check("google", "Google account", OK, proven.detail,
                         verified=True)
        if not proven.ran:
            return Check("google", "Google account", UNKNOWN, proven.detail,
                         fix=proven.evidence or "Try testing it again.",
                         action="verify")
        return Check("google", "Google account", ATTENTION, proven.detail,
                     fix="Sign in again — the saved session no longer works.",
                     action="connect")

    has_state = s.storage_state.exists()
    if has_state:
        return Check("google", "Google account", UNKNOWN,
                     "Session found, but not tested yet.",
                     fix="Test it — a saved cookie is not proof the account "
                         "still works.",
                     action="verify")

    # No storage_state, so the tool has nothing to use either way. What the
    # profile says only changes what the person should DO about it.
    cookie = profile_has_google_session(s.profile_dir)
    if cookie is None and s.profile_dir.exists():
        # Chrome is holding the cookie store. "Not connected" here would send
        # someone back through a sign-in they may have just finished.
        return Check("google", "Google account", UNKNOWN,
                     "Cannot read the profile — Chrome may still be open.",
                     fix="Close Chrome, then test again.", action="verify")
    if cookie:
        return Check("google", "Google account", ATTENTION,
                     "Signed in, but the session was never saved.",
                     fix="Close Chrome and press Connect — it captures the "
                         "session without a second sign-in.",
                     action="connect")
    return Check("google", "Google account", ATTENTION, "Not connected.",
                 fix="Sign in to Google once, in a real Chrome window.",
                 action="connect")


def _untappd_check(s: Settings, proven: VerifyResult | None) -> Check:
    from ..chrome_launch import profile_has_untappd_session

    if proven is not None:
        if proven.ok:
            return Check("untappd", "Untappd account", OK, proven.detail,
                         verified=True)
        if not proven.ran:
            return Check("untappd", "Untappd account", UNKNOWN, proven.detail,
                         fix=proven.evidence or "Try testing it again.",
                         action="verify")
        return Check("untappd", "Untappd account", ATTENTION, proven.detail,
                     fix="Sign in to untappd.com in the same Chrome window.",
                     action="connect")

    # A profile that does not exist has never been to untappd.com. That is a
    # real answer, not an unreadable one, and the difference matters: a fresh
    # machine should say "not connected", not "cannot tell".
    if not s.profile_dir.exists():
        return Check("untappd", "Untappd account", ATTENTION,
                     "Not connected. Signed out, search stops at 5 results.",
                     fix="Sign in to untappd.com in the same Chrome window.",
                     action="connect")

    cookie = profile_has_untappd_session(s.profile_dir)
    if cookie is False:
        return Check("untappd", "Untappd account", ATTENTION,
                     "Not connected. Signed out, search stops at 5 results.",
                     fix="Sign in to untappd.com in the same Chrome window.",
                     action="connect")
    if cookie is None:
        return Check("untappd", "Untappd account", UNKNOWN,
                     "Cannot tell from the profile alone.",
                     fix="Test it — one request settles it.", action="verify")
    return Check("untappd", "Untappd account", UNKNOWN,
                 "Session found, but not tested yet.",
                 fix="Test it — one request settles it.", action="verify")


def _keys_check(s: Settings) -> tuple[Check, ...]:
    geo = Check("geocoding", "Geocoding", OK,
                "Google" if s.google_geocoding_key
                else "Nominatim — free, 1 request a second", verified=True)
    places = Check(
        "places", "Closure check",
        OK if s.google_places_key else OFF,
        "Google Places" if s.google_places_key
        else "Off. Set GOOGLE_PLACES_KEY to check whether venues still trade.",
        verified=bool(s.google_places_key),
    )
    return (geo, places)


# Where to go, and the longer answer, keyed by row. Attached in one place
# rather than at each branch: every row has several outcomes and the links
# do not vary between them, so repeating them per branch is how one gets
# quietly dropped from the branch nobody tested.
REFERENCE: dict[str, tuple[tuple[tuple[str, str], ...], str]] = {
    "chrome": (
        (("Download Chrome", "https://www.google.com/chrome/"),),
        "Google refuses to complete a sign-in inside a browser that reports "
        "itself as automated — it answers \"This browser or app may not be "
        "secure\". So the login runs in an ordinary Chrome process against a "
        "profile folder inside this project, and the automation reuses that "
        "session afterwards. Google blocks the sign-in flow, not a session "
        "that already exists.",
    ),
    "playwright": (
        (("What Playwright is", "https://playwright.dev/python/docs/intro"),),
        "Playwright drives the browser that captures your sign-in and, later, "
        "saves places into a list. Nothing that only reads Untappd needs it, "
        "which is why a plain install leaves it out.",
    ),
    "google": (
        (("Your Google account", "https://myaccount.google.com/"),
         ("Create an account", "https://accounts.google.com/signup")),
        "The Google session is what lets this save places into one of your "
        "Maps lists and upload a map layer. Collecting the venue data needs "
        "none of it — so a failure here costs you the map, not the data.",
    ),
    "untappd": (
        (("Sign in to Untappd", "https://untappd.com/login"),
         ("Create an account", "https://untappd.com/signup")),
        "Untappd shows a signed-out visitor only 5 search results. This is "
        "the one that quietly ruins a run: the scrape finishes, reports "
        "success, and hands you five venues instead of a hundred — with the "
        "diff, the baseline and the map all built on top of them. It also "
        "fills the YOU column, which of these you have already checked into.",
    ),
    "geocoding": (
        (("Geocoding pricing",
          "https://developers.google.com/maps/documentation/geocoding/usage-and-billing"),
         ("Nominatim usage policy",
          "https://operations.osmfoundation.org/policies/nominatim/")),
        "Most Untappd venues carry their own coordinates, so this only runs "
        "for the ones that do not. Without a key it falls back to Nominatim "
        "at one request a second, which is free and slower.",
    ),
    "places": (
        (("Enable the Places API",
          "https://console.cloud.google.com/apis/library/places.googleapis.com"),
         ("What it costs",
          "https://developers.google.com/maps/billing-and-pricing/pricing")),
        "Optional. With a key, `closures` asks Google whether each venue "
        "still trades — Untappd keeps a page for a bar that shut in 2019, and "
        "its lifetime check-ins make it outrank a good bar that opened last "
        "year. 5,000 lookups a month are free. Venues Places cannot match are "
        "recorded as unmatched, never as closed.",
    ),
}


def _with_reference(check: Check) -> Check:
    """Attach the row's links and long answer, if it has any."""
    entry = REFERENCE.get(check.key)
    if entry is None:
        return check
    links, why = entry
    return replace(check, links=links, why=why)


def collect(s: Settings,
            proven: dict[str, VerifyResult] | None = None) -> tuple[Check, ...]:
    """Every check, in the order a first-time user meets them.

    Offline and free. `proven` carries the results of any live verification
    already run this session; without it, the two account rows report what
    the filesystem shows and say plainly that it has not been tested.
    """
    proven = proven or {}
    rows = (
        _chrome_check(),
        _playwright_check(),
        _google_check(s, proven.get("google")),
        _untappd_check(s, proven.get("untappd")),
        *_keys_check(s),
    )
    return tuple(_with_reference(c) for c in rows)


def blocking(checks: tuple[Check, ...]) -> tuple[Check, ...]:
    """The rows standing between this user and a working tool."""
    return tuple(c for c in checks if c.state == ATTENTION)


def ready(checks: tuple[Check, ...]) -> bool:
    """Both accounts verified by round-trip, and the machine can run them.

    Deliberately strict about `verified`. A cookie on disk is not a working
    account, and the whole reason this dashboard exists is that finding out
    otherwise used to happen a hundred requests into a run.
    """
    by_key = {c.key: c for c in checks}
    return all(by_key[k].verified for k in ("chrome", "google", "untappd")
               if k in by_key)



@dataclass(frozen=True)
class NextStep:
    """The one thing to do now, and nothing else.

    A checklist tells a new user what is wrong. It does not tell them what to
    do, and six rows of detail is worse than one instruction when five of them
    are not actionable yet. So the panel is the evidence and this is the
    directive -- exactly one, chosen in the order a person actually hits them.
    """

    key: str
    title: str
    body: str
    cta: str = ""       # button label; empty when the user acts elsewhere
    action: str = ""    # the action id that button starts
    done: bool = False


def next_step(rows: tuple[Check, ...]) -> NextStep:
    """What to do now. Ordered by what blocks what."""
    by_key = {c.key: c for c in rows}

    if by_key["chrome"].state == ATTENTION:
        return NextStep(
            "chrome", "Install Google Chrome",
            "The sign-in has to happen in a real Chrome window — Google "
            "refuses to complete a login inside an automated browser.",
        )
    if by_key["playwright"].state == ATTENTION:
        return NextStep(
            "playwright", "Install the browser tooling",
            'Run pip install -e ".[browser]" in the project folder, then '
            "reload this page.",
        )

    accounts = (by_key["google"], by_key["untappd"])
    if any(c.state == ATTENTION for c in accounts):
        # The row labels are "Google account" / "Untappd account", which read
        # as "Sign in to Untappd account" once joined into a sentence.
        short = {"google": "Google", "untappd": "Untappd"}
        missing = [short[c.key] for c in accounts if c.state == ATTENTION]
        return NextStep(
            "connect", "Sign in to your accounts",
            f"Chrome will open. Sign in to {' and '.join(missing)}, then close "
            f"the window — closing it is how you say you're done. No account "
            f"yet? The links below will make one.",
            cta="Open Chrome and sign in", action="connect",
        )
    if not all(c.verified for c in accounts):
        return NextStep(
            "verify", "Test that both accounts work",
            "A saved cookie means a login happened once, not that it still "
            "works. This checks each one for real — a headless Maps load, and "
            "one request to Untappd.",
            cta="Test both accounts", action="verify",
        )

    return NextStep(
        "run", "Name your city",
        "Both accounts are working. Type the city you want, then either press "
        "Use this city — your agent will pick it up — or copy the command and "
        "run it yourself. Either way the run happens in your terminal, where "
        "you can watch it and stop it.",
        done=True,
    )


# Three steps, and the grouping is the simplification. The panel has six rows
# because six things can be wrong; a person setting this up for the first time
# has three questions, in this order: can this machine do it, are my accounts
# connected, and what city do I want. `next_step` already picks the one thing
# to do -- this says which of the three it belongs to, so the page can show a
# position instead of a list.
WIZARD = (
    ("ready", "Ready", ("chrome", "playwright")),
    ("accounts", "Accounts", ("connect", "verify")),
    ("city", "City", ("run",)),
)


@dataclass(frozen=True)
class Stage:
    """One dot in the stepper."""

    key: str
    label: str
    state: str   # done | current | todo

    def to_row(self) -> dict:
        return {"key": self.key, "label": self.label, "state": self.state}


def wizard(step: NextStep) -> tuple[Stage, ...]:
    """The three stages, with the current one marked.

    Derived from `next_step` rather than computed separately: two functions
    deciding where the user is, from the same rows, is two chances to
    disagree -- and the one that disagrees is always the one on screen.
    """
    current = next((i for i, (_, _, keys) in enumerate(WIZARD)
                    if step.key in keys), len(WIZARD) - 1)
    out = []
    for i, (key, label, _) in enumerate(WIZARD):
        if i < current:
            state = "done"
        elif i == current:
            state = "done" if step.done else "current"
        else:
            state = "todo"
        out.append(Stage(key, label, state))
    return tuple(out)

# --------------------------------------------------------------------------
# Tier 2: verification. A real round-trip each.
# --------------------------------------------------------------------------
def verify_google(s: Settings) -> VerifyResult:
    """Open the saved session headlessly and see if Google still knows us.

    This is what `_capture_session` already does at the end of `bootstrap`,
    asked as a question rather than as a side effect -- so a session that
    expired two weeks after bootstrap is caught here instead of by a `pin`.
    """
    if not s.storage_state.exists():
        return VerifyResult(False, "No saved session to test.",
                            evidence=f"{s.storage_state} does not exist")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return VerifyResult(False, "Playwright is not installed.",
                            evidence='Run: pip install -e ".[browser]"',
                            ran=False)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            try:
                ctx = browser.new_context(storage_state=str(s.storage_state),
                                          user_agent=s.user_agent)
                page = ctx.new_page()
                page.goto("https://www.google.com/maps",
                          wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(4000)
                signed_in = page.locator(
                    "a[aria-label*='Google Account'], img[alt*='Google Account']"
                ).count() > 0
                ctx.close()
            finally:
                browser.close()
    except Exception as exc:
        # A browser that will not start is not a signed-out account, and
        # reporting it as one would send the user into a login they do not
        # need. Say what actually happened.
        log.error("google verification could not run: %s", exc)
        return VerifyResult(False, "Could not run the check.",
                            evidence=f"{type(exc).__name__}: {exc}",
                            ran=False)

    if signed_in:
        return VerifyResult(True, "Signed in — checked just now.",
                            evidence="Google Account control present on Maps")
    return VerifyResult(False, "The saved session is no longer signed in.",
                        evidence="No Google Account control on Maps")


# Signed out, Untappd's own pages still render; what changes is the header.
# These are the markers that only appear for a signed-in user.
_UNTAPPD_SIGNED_IN = ('href="/user/', "user-menu", "Sign Out", "sign-out")


def verify_untappd(s: Settings) -> VerifyResult:
    """One authenticated request to Untappd, to see whose session it is.

    Cheap on purpose -- a single GET of the home page, carrying the cookies
    `run` itself would carry. That is the thing worth testing: not whether a
    cookie exists, but whether the session this tool will actually use is
    recognised.

    What it does not prove is the 5-result search cap, which is the symptom
    the user eventually feels. Proving that needs a search, and a search
    against a signed-out session is exactly the request this check exists to
    avoid making a hundred times.
    """
    import httpx

    from ..http_client import _cookies_from_storage_state

    cookies = _cookies_from_storage_state(s.storage_state, "untappd.com")
    if not cookies:
        return VerifyResult(False, "No Untappd cookies in the saved session.",
                            evidence="storage_state carries no untappd.com "
                                     "cookies")
    try:
        with httpx.Client(timeout=s.request_timeout_s, follow_redirects=True,
                          headers={"User-Agent": s.user_agent},
                          cookies=cookies) as client:
            r = client.get("https://untappd.com/")
    except Exception as exc:
        log.error("untappd verification could not run: %s", exc)
        return VerifyResult(False, "Could not reach Untappd.",
                            evidence=f"{type(exc).__name__}: {exc}",
                            ran=False)

    if r.status_code != 200:
        return VerifyResult(False, f"Untappd answered HTTP {r.status_code}.",
                            evidence=f"GET / -> {r.status_code}")
    hit = next((m for m in _UNTAPPD_SIGNED_IN if m in r.text), None)
    if hit:
        return VerifyResult(True, "Signed in — checked just now.",
                            evidence=f"page carries {hit!r}")
    return VerifyResult(False, "Reached Untappd, but signed out.",
                        evidence="no signed-in marker in the home page")


VERIFIERS = {"google": verify_google, "untappd": verify_untappd}


def with_state(check: Check, state: str, detail: str) -> Check:
    """Immutable update, for the server's in-flight 'checking…' rendering."""
    return replace(check, state=state, detail=detail)
