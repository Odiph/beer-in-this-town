"""The slow things the dashboard can start, narrated as they run.

Each function here is the `work` half of a `jobs.Job`: it takes a `say`
callback and returns a result dict. Nothing in this module writes to a Google
account -- the dashboard's scope stops at getting a user connected, so `pin`
and `notes` have no entry point here at all, and that is a deliberate absence
rather than an oversight.

The narration is the feature. A person watching a blank screen for four
minutes cannot tell a working sign-in from a hung one, and the two need
opposite responses.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..config import Settings
from .checks import VERIFIERS, VerifyResult

log = logging.getLogger(__name__)

Say = Callable[..., None]
Mark = Callable[[str, object], None]

# How long to wait for a person to finish signing in before giving up on the
# window. Matches `bootstrap`. Chrome is never killed at the end of it: the
# user may be mid-login, and killing Chrome can corrupt the profile.
LOGIN_TIMEOUT_S = 900.0
POLL_S = 3.0


def connect(s: Settings, say: Say, mark: Mark) -> dict:
    """Open a real Chrome, wait for both logins, then capture the session.

    Both accounts in one window, on purpose. They are two logins with one
    shared prerequisite -- a browser Google will accept -- and sending the
    user round this loop twice is how the Untappd half gets skipped, which is
    precisely the omission `search_login_required` reports much later.
    """
    from ..chrome_launch import (
        find_chrome,
        launch_for_login,
        profile_has_google_session,
        profile_has_untappd_session,
    )

    if find_chrome() is None:
        raise RuntimeError(
            "Could not find Google Chrome. The sign-in has to happen in a "
            "real Chrome window — Google refuses logins in automated ones."
        )

    say("Opening a normal Chrome window.")
    say("Not an automated one — Google refuses to complete a sign-in inside "
        "an automation-controlled browser, so this is a plain Chrome process "
        "using our own profile directory.", aside=True)

    proc = launch_for_login(s.profile_dir)
    if proc is None:  # pragma: no cover -- guarded by find_chrome above
        raise RuntimeError("Chrome could not be started.")

    say("Two tabs have opened in that window: Google and Untappd. "
        "Sign in to both, in that window.")
    say("It has to be that window — it uses a separate profile, so signing in "
        "to your everyday Chrome does nothing here. Untappd is the one that "
        "gets missed, and signed out its search stops at 5 results.", aside=True)
    say("Then close the window. That is the finish signal, and nothing here "
        "can see your progress until you do: Chrome keeps the profile locked "
        "while it runs.")

    # Best-effort, and on Windows usually silent: Chrome holds an exclusive
    # lock on the cookie store while it runs, so these probes return None --
    # unknown -- for the whole time the window is open, which is exactly when
    # a person wants reassurance. That is why the instruction above says
    # nothing can be seen until the window closes, rather than promising a
    # tick that will not arrive.
    mark("waiting_for", ["google", "untappd"])
    mark("google", False)
    mark("untappd", False)

    deadline = time.time() + LOGIN_TIMEOUT_S
    seen = {"google": False, "untappd": False}
    while time.time() < deadline:
        if proc.poll() is not None:
            say("Chrome closed.")
            break
        if not seen["google"] and profile_has_google_session(s.profile_dir):
            seen["google"] = True
            mark("google", True)
            say("Google sign-in detected.")
        if not seen["untappd"] and profile_has_untappd_session(s.profile_dir) is True:
            seen["untappd"] = True
            mark("untappd", True)
            say("Untappd sign-in detected.")
        time.sleep(POLL_S)
    else:
        raise RuntimeError(
            f"Chrome was still open after {LOGIN_TIMEOUT_S / 60:.0f} minutes. "
            f"Close the window and press Connect again — nothing is lost."
        )

    if not seen["untappd"]:
        # Not fatal. The cookie-name list behind that check is unverified, so
        # a miss here means "could not tell", and the verification below is
        # what actually settles it.
        say("No Untappd sign-in seen while the window was open — the check "
            "after this will settle it either way.", aside=True)

    mark("waiting_for", [])
    say("Reading the session out of the profile.")
    say("Chrome holds an exclusive lock on the profile while it runs, which "
        "is why this waits for the window to close.", aside=True)
    time.sleep(2)  # let Chrome flush its cookie store to disk
    captured = _capture(s)
    say(f"Saved the session to {s.storage_state.name}.")

    return {"captured": captured, **_verify_both(s, say, mark)}


def _capture(s: Settings) -> bool:
    """Write storage_state.json from the profile Chrome has just released."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(s.profile_dir), channel="chrome", headless=True,
        )
        try:
            ctx.storage_state(path=str(s.storage_state))
        finally:
            ctx.close()
    return s.storage_state.exists()


def _verify_both(s: Settings, say: Say, mark: Mark) -> dict:
    out = {}
    for key in ("google", "untappd"):
        result = verify_one(s, say, key)
        mark(key, result.ok)
        out[key] = result.__dict__
    return out


def verify_one(s: Settings, say: Say, which: str) -> VerifyResult:
    """Prove one account works, end to end, rather than sniffing for a cookie.

    A cookie says a login happened once. This says the account works now,
    which is the only version of "connected" worth showing a user.
    """
    verifier = VERIFIERS.get(which)
    if verifier is None:
        raise RuntimeError(f"Nothing to verify called {which!r}.")

    if which == "google":
        say("Testing the Google session.")
        say("Opening Google Maps headlessly with the saved cookies and looking "
            "for the account control. A saved cookie is not proof the account "
            "still works — sessions expire.", aside=True)
    else:
        say("Testing the Untappd session.")
        say("One request to untappd.com, carrying exactly the cookies a real "
            "run would carry.", aside=True)

    result = verifier(s)
    say(("Verified: " if result.ok else "Failed: ") + result.detail)
    if result.evidence:
        say(result.evidence, aside=True)
    return result


def verify_accounts(s: Settings, say: Say, mark: Mark) -> dict:
    """Both accounts, end to end. What a new user runs before trusting any of it."""
    say("Checking both accounts end to end.")
    mark("waiting_for", ["google", "untappd"])
    results = {}
    for key in ("google", "untappd"):
        result = verify_one(s, say, key)
        mark(key, result.ok)
        results[key] = result.__dict__
    mark("waiting_for", [])
    passed = sum(1 for r in results.values() if r["ok"])
    say(f"{passed} of 2 accounts verified.")
    return results


def check_selectors(s: Settings, say: Say, mark: Mark) -> dict:
    """Two requests: is the tool still able to read Untappd's pages?

    Separate from the accounts, and worth its own button, because it fails for
    a completely different reason -- the site changed -- and the fix is a code
    change rather than anything the user can do in a browser.
    """
    from ..cli import cmd_selfcheck

    say("Checking the venue page and the search page.")
    say("Two paced requests. The gap between them is deliberate — a fixed, "
        "fast cadence is what makes a client look like a script.", aside=True)

    env = cmd_selfcheck(s, "american-taproom-waterloo", "7480946",
                        probe_search=True)
    if env.ok:
        say("Both pages parsed. Selectors are alive.")
    else:
        say(f"Failed: {env.error.message if env.error else 'unknown'}")
        say("This one is not something you can fix in a browser — it means "
            "Untappd changed its markup and the parser needs updating.",
            aside=True)
    return {"ok": env.ok, "data": env.data,
            "error": env.error.code if env.error else ""}


ACTIONS: dict[str, tuple[str, Callable[..., dict]]] = {
    "connect": ("Connecting your accounts", connect),
    "verify": ("Testing both accounts", verify_accounts),
    "selectors": ("Checking Untappd's pages", check_selectors),
}
