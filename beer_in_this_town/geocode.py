"""Address -> lat/lng, but only for venues Untappd did not already hand us.

Cost note: Google Geocoding is $5.00/1000 with a 10,000/month free allowance on
the Geocoding Essentials SKU (per-SKU free tiers replaced the old shared $200
credit in March 2025). At ~100 venues/week with most coords embedded, expect
single-digit paid calls per run -- i.e. $0.00 -- but billing must be enabled on
the key regardless. Set GOOGLE_GEOCODING_KEY to use it; otherwise the script
falls back to Nominatim at a strict 1 request/second.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from . import PROJECT_URL, __version__
from .config import STATE_DIR, Settings
from .models import Venue

log = logging.getLogger(__name__)

# Google's Maps Platform terms allow caching a geocoded lat/lng for at most 30
# consecutive calendar days. Nominatim's data is ODbL: an entry derived from it
# may be kept indefinitely (with attribution), so only Google's expire.
GOOGLE_CACHE_TTL_S = 30 * 24 * 60 * 60
SOURCE_GOOGLE = "google"
SOURCE_NOMINATIM = "nominatim"

# Injected so the expiry is testable without waiting a month.
_now = time.time

# Nominatim's usage policy asks every caller to identify itself with a working
# contact. The fallback used to be the literal string "no-contact-set", sent
# silently; now it is the project URL, and the missing contact is said out loud
# once per process.
_warned_no_contact = False


def _user_agent(s: Settings) -> str:
    contact = s.nominatim_email or f"+{PROJECT_URL}"
    return f"beer-in-this-town/{__version__} ({contact})"


def _warn_if_no_contact(s: Settings) -> None:
    """Warn once when Nominatim is about to be called with no contact set."""
    global _warned_no_contact
    if s.google_geocoding_key or s.nominatim_email or _warned_no_contact:
        return
    _warned_no_contact = True
    log.warning(
        "NOMINATIM_EMAIL is not set. OpenStreetMap's Nominatim asks every "
        "caller for a contact address so it can reach you before blocking "
        "you; set NOMINATIM_EMAIL to your email. Continuing without one."
    )


class GeocoderUnavailable(RuntimeError):
    """The geocoder itself is not working -- key, quota, billing or network.

    Distinct from a venue that simply has no match. The difference decides
    whether one venue loses its pin or the whole run should stop, and
    collapsing the two is how a rejected API key ships a KML with four
    placemarks instead of a hundred and still reports success.
    """


GEOCACHE = STATE_DIR / "geocache.json"
GOOGLE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def _entry(lat: float, lng: float, source: str) -> dict:
    return {"lat": lat, "lng": lng, "source": source, "fetched_at": _now()}


def _is_live(entry: object, now: float) -> bool:
    """Whether a cache entry may still be served.

    A bare `[lat, lng]` is the pre-0.2 format and carries no provenance. It may
    have come from Google, so it is treated as expired: re-fetching once costs
    a request; serving Google data past its 30 days breaks their terms.
    """
    if not isinstance(entry, dict):
        return False
    try:
        float(entry["lat"])
        float(entry["lng"])
    except (KeyError, TypeError, ValueError):
        return False
    if entry.get("source") == SOURCE_NOMINATIM:
        return True
    fetched_at = entry.get("fetched_at")
    if not isinstance(fetched_at, int | float):
        return False
    return now - fetched_at < GOOGLE_CACHE_TTL_S


def _load_cache() -> dict[str, dict]:
    """Live entries only. Expired ones are dropped here, and so off the disk
    at the next save."""
    if not GEOCACHE.exists():
        return {}
    try:
        raw = json.loads(GEOCACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("geocode cache at %s is unreadable (%s) -- starting empty.",
                    GEOCACHE, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    now = _now()
    return {q: e for q, e in raw.items() if _is_live(e, now)}


def _save_cache(cache: dict[str, dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = _now()
    live = {q: e for q, e in cache.items() if _is_live(e, now)}
    GEOCACHE.write_text(json.dumps(live, indent=1), encoding="utf-8")


def _query_for(v: Venue) -> str:
    parts = [v.ref.name, v.ref.address, v.ref.city]
    return ", ".join(p for p in parts if p)


def _google(client: httpx.Client, key: str, query: str) -> tuple[float, float] | None:
    r = client.get(GOOGLE_URL, params={"address": query, "key": key})
    r.raise_for_status()
    data = r.json()
    status = data.get("status")
    if status == "ZERO_RESULTS":
        return None
    if status != "OK":
        # REQUEST_DENIED / OVER_QUERY_LIMIT are configuration or billing
        # problems -- surface them, do not quietly degrade.
        raise GeocoderUnavailable(
            f"Google Geocoding returned {status}: {data.get('error_message')}"
        )
    loc = data["results"][0]["geometry"]["location"]
    return float(loc["lat"]), float(loc["lng"])


def _nominatim(
    client: httpx.Client, query: str, email: str | None
) -> tuple[float, float] | None:
    params: dict[str, object] = {"q": query, "format": "jsonv2", "limit": 1}
    if email:
        params["email"] = email
    r = client.get(NOMINATIM_URL, params=params)
    r.raise_for_status()
    results = r.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def geocode_missing(venues: list[Venue], s: Settings) -> list[Venue]:
    """Return a NEW list with coordinates filled in where they were missing."""
    cache = _load_cache()
    todo = [v for v in venues if not v.has_coords]
    if not todo:
        log.info("All %d venues had embedded coordinates -- no geocoding needed.",
                 len(venues))
        return list(venues)

    log.info("Geocoding %d/%d venues without embedded coordinates",
             len(todo), len(venues))
    resolved: dict[str, tuple[float, float, str]] = {}
    attempted = errored = 0

    ua = {"User-Agent": _user_agent(s)}
    with httpx.Client(timeout=20.0, headers=ua) as client:
        for v in todo:
            query = _query_for(v)
            if not query:
                continue
            if query in cache:
                cached = cache[query]
                resolved[v.ref.venue_id] = (float(cached["lat"]),
                                            float(cached["lng"]), "cache")
                continue
            _warn_if_no_contact(s)
            attempted += 1
            using_osm = not s.google_geocoding_key
            try:
                if s.google_geocoding_key:
                    hit = _google(client, s.google_geocoding_key, query)
                    source = SOURCE_GOOGLE
                else:
                    hit = _nominatim(client, query, s.nominatim_email)
                    source = SOURCE_NOMINATIM
            except GeocoderUnavailable:
                # Never per-venue: the next hundred lookups would fail the same
                # way. Stop instead of dropping a pin a hundred times.
                raise
            except Exception as exc:
                log.error("geocode failed for %r: %s", query, exc)
                errored += 1
                continue
            finally:
                # OSM policy is at most one request a second, and the pause
                # used to sit after the call inside the `try`: a timeout or a
                # 429 skipped it, so consecutive failures hit Nominatim
                # back-to-back. Being throttled is exactly when pacing stops
                # being optional.
                if using_osm:
                    time.sleep(s.nominatim_delay_s)
            if hit is None:
                log.warning("no geocode result for %r", query)
                continue
            cache[query] = _entry(hit[0], hit[1], source)
            resolved[v.ref.venue_id] = (hit[0], hit[1], source)

    _save_cache(cache)

    # Every single lookup erroring is not a hundred unlucky addresses; it is
    # the geocoder being unreachable, blocked or rate-limited. A venue with no
    # match does not reach this count -- that path returns None rather than
    # raising -- so this cannot fire on a genuinely hard batch.
    if attempted and errored == attempted:
        raise GeocoderUnavailable(
            f"All {attempted} geocoding request(s) failed. The geocoder is "
            f"unreachable, blocked or misconfigured."
        )

    return [
        v.with_coords(*resolved[v.ref.venue_id]) if v.ref.venue_id in resolved else v
        for v in venues
    ]


def geocode_place(query: str, s: Settings) -> tuple[float, float] | None:
    """One place name -> `(lat, lng)`, or None when the geocoder has no match.

    Used for a city, where the sweep needs a starting centre. A failure of the
    geocoder itself raises `GeocoderUnavailable` rather than returning None:
    "no such place" and "the service is down" want opposite remedies.
    """
    cache = _load_cache()
    if query in cache:
        return float(cache[query]["lat"]), float(cache[query]["lng"])

    _warn_if_no_contact(s)
    ua = {"User-Agent": _user_agent(s)}
    try:
        with httpx.Client(timeout=20.0, headers=ua) as client:
            if s.google_geocoding_key:
                hit = _google(client, s.google_geocoding_key, query)
                source = SOURCE_GOOGLE
            else:
                hit = _nominatim(client, query, s.nominatim_email)
                source = SOURCE_NOMINATIM
    except GeocoderUnavailable:
        raise
    except Exception as exc:
        raise GeocoderUnavailable(f"Could not geocode {query!r}: {exc}") from exc

    if hit is None:
        return None
    _save_cache({**cache, query: _entry(hit[0], hit[1], source)})
    return hit
