"""Central, immutable configuration. Nothing downstream hardcodes a value."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state"
CACHE_DIR = ROOT / "cache"
DEBUG_DIR = ROOT / "debug"

BASE = "https://untappd.com"
SEARCH_URL = f"{BASE}/search"

# A current, real Chrome UA. Keep this in sync with the Chrome you actually run;
# a stale UA is a cheap tell.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Settings:
    """Immutable settings object. Use `replace(settings, field=value)` to derive."""

    # --- what to scrape -------------------------------------------------
    query: str = "singapore"
    target_count: int = 100
    map_title: str = "Singapore Bars"

    # --- session --------------------------------------------------------
    profile_dir: Path = ROOT / "chrome-profile"
    storage_state: Path = ROOT / "storage_state.json"
    user_agent: str = DEFAULT_UA

    # --- politeness -----------------------------------------------------
    min_delay_s: float = 2.0
    max_delay_s: float = 4.5
    hourly_budget: int = 600
    max_retries: int = 3
    backoff_ladder_s: tuple[int, ...] = (60, 180, 600)
    max_consecutive_429: int = 3
    cache_ttl_s: int = 12 * 3600
    request_timeout_s: float = 30.0

    # --- correctness gates ----------------------------------------------
    parse_strictness: float = 0.90  # fraction of venues that must yield full stats
    respect_robots: bool = True

    # --- geocoding ------------------------------------------------------
    google_geocoding_key: str | None = None
    nominatim_email: str | None = None
    nominatim_delay_s: float = 1.1  # OSM policy: max 1 req/sec

    @staticmethod
    def from_env() -> Settings:
        s = Settings()
        return replace(
            s,
            google_geocoding_key=os.environ.get("GOOGLE_GEOCODING_KEY") or None,
            nominatim_email=os.environ.get("NOMINATIM_EMAIL") or None,
        )


def ensure_dirs() -> None:
    for d in (DATA_DIR, STATE_DIR, CACHE_DIR, DEBUG_DIR):
        d.mkdir(parents=True, exist_ok=True)
