"""Central, immutable configuration. Nothing downstream hardcodes a value."""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state"
CACHE_DIR = ROOT / "cache"
DEBUG_DIR = ROOT / "debug"

BASE = "https://untappd.com"
SEARCH_URL = f"{BASE}/search"

# A real Chrome UA. Chrome has frozen the minor/build/patch fields at 0.0.0
# since v107, so the major version is the only part that varies.
UA_TEMPLATE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)

# Fallback only, for when no Chrome can be found to ask. Being a fallback is
# the point: this comment used to say "keep this in sync with the Chrome you
# actually run, a stale UA is a cheap tell" -- and then the number sat at 128
# while the installed Chrome reached 151, because nothing made it happen.
# `user_agent_for_installed_chrome` now keeps it honest without anyone
# remembering to.
FALLBACK_CHROME_MAJOR = "151"
DEFAULT_UA = UA_TEMPLATE.format(major=FALLBACK_CHROME_MAJOR)


def user_agent_for_installed_chrome() -> str:
    """Claim the Chrome that is actually installed here.

    Consistency is the whole value: the requests already carry Chrome's fetch
    metadata and ride a session created by a real Chrome, so naming a version
    that no longer exists is the one part that would not add up.
    """
    from .chrome_launch import chrome_major_version

    major = chrome_major_version()
    return UA_TEMPLATE.format(major=major) if major else DEFAULT_UA


@dataclass(frozen=True)
class Settings:
    """Immutable settings object. Use `replace(settings, field=value)` to derive."""

    # --- what to scrape -------------------------------------------------
    # No default city, deliberately. A default here does not save anyone a
    # keystroke -- it silently answers a question only the user can answer,
    # and the answer it gives is a scrape of somewhere they have never been.
    # Same reasoning as `logged_in`: a stand-in for a decision reads exactly
    # like the decision having been made.
    query: str = ""
    target_count: int = 100
    map_title: str = ""

    # --- session --------------------------------------------------------
    profile_dir: Path = ROOT / "chrome-profile"
    storage_state: Path = ROOT / "storage_state.json"
    user_agent: str = DEFAULT_UA

    # --- politeness -----------------------------------------------------
    # Every number here is a defence, not a preference. `http_client.py`'s
    # module docstring explains what each one is defending against and why
    # raising it is not free -- read that before touching any of them.
    min_delay_s: float = 2.0      # jittered gap between requests, lower bound
    max_delay_s: float = 4.5      # ...and upper. Jitter matters: fixed is a tell
    hourly_budget: int = 600      # hard per-hour ceiling; raises, never sleeps
    max_retries: int = 3
    backoff_ladder_s: tuple[int, ...] = (60, 180, 600)  # climb on 429/503
    max_consecutive_429: int = 3  # then abort: three in a row means stop, not wait
    cache_ttl_s: int = 12 * 3600  # a re-run costs ~no requests
    request_timeout_s: float = 30.0

    # --- correctness gates ----------------------------------------------
    parse_strictness: float = 0.90  # fraction of venues that must yield full stats
    respect_robots: bool = True     # refuse to start if robots.txt says no

    # --- geocoding ------------------------------------------------------
    google_geocoding_key: str | None = None
    nominatim_email: str | None = None
    nominatim_delay_s: float = 1.1  # OSM policy: max 1 req/sec

    # --- geographic collection (Overpass / OSM) -------------------------
    # No key, no billing, no quota -- which is why this is the preferred
    # provider for "what is near this point" over Places. Overridable because
    # the public endpoint is volunteer-run and sheds load; a mirror or a
    # self-hosted instance is a legitimate answer to being throttled.
    overpass_url: str = "https://overpass-api.de/api/interpreter"

    # --- the app sweep (emulator) ---------------------------------------
    # Which adb device the sweep drives. Every adb call is addressed by
    # serial because a BlueStacks config can put two instances on one port.
    adb_serial: str = "127.0.0.1:5555"

    # --- closure check (#7) ---------------------------------------------
    # Deliberately a separate key from geocoding: different SKU, and somebody
    # may reasonably want coordinates without sending addresses to Places for
    # classification. Absent means the stage does not run -- never that it
    # runs and guesses.
    google_places_key: str | None = None

    @staticmethod
    def from_env() -> Settings:
        s = Settings()
        return replace(
            s,
            user_agent=user_agent_for_installed_chrome(),
            google_geocoding_key=os.environ.get("GOOGLE_GEOCODING_KEY") or None,
            google_places_key=os.environ.get("GOOGLE_PLACES_KEY") or None,
            nominatim_email=os.environ.get("NOMINATIM_EMAIL") or None,
            overpass_url=os.environ.get("OVERPASS_URL") or s.overpass_url,
            adb_serial=os.environ.get("BEERTOWN_ADB_SERIAL") or s.adb_serial,
        )


SCOPE_FALLBACK = "unnamed"


def scope_slug(value: str) -> str:
    """Turn a city or list name into a filename-safe key.

    State files are named after the thing they describe, which means a user
    string reaches the filesystem. Stripping everything that is not a letter,
    digit or hyphen is what stops a list called "../../etc/passwd" from writing
    outside `state/`; collapsing case and spaces is what stops "Singapore Bars"
    and "singapore bars" from keeping two half-complete journals of the same
    list.
    """
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    # A name of pure punctuation would otherwise produce "", and every such
    # list would then share one journal named after nothing.
    return slug or SCOPE_FALLBACK


def ensure_dirs() -> None:
    for d in (DATA_DIR, STATE_DIR, CACHE_DIR, DEBUG_DIR):
        d.mkdir(parents=True, exist_ok=True)
