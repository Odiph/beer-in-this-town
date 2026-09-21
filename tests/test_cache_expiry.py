"""Google's terms put a clock on what we keep; OpenStreetMap's do not.

Geocoding: a lat/lng from Google may be cached for 30 days, one from Nominatim
indefinitely. Places: a place ID may be kept forever, the rest of the reply
(name, types, business status) for 30 days. A stale entry must not be served,
and must not sit on disk either. No network.
"""
from __future__ import annotations

import json

import pytest

from beer_in_this_town import geocode, places
from beer_in_this_town.config import Settings
from beer_in_this_town.models import Venue, VenueRef
from beer_in_this_town.places import PlaceMatch

pytestmark = pytest.mark.unit

DAY = 24 * 60 * 60
T0 = 1_800_000_000.0


class _NullClient:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _venue(vid: str = "1", name: str = "Ghost Whale") -> Venue:
    return Venue(
        ref=VenueRef(venue_id=vid, slug=name.lower(), name=name,
                     category="Beer Bar", address=f"{vid} Some Road", city="London"),
        total=1, unique=1, monthly=1, you=None,
    )


# --- geocoding -----------------------------------------------------------

@pytest.fixture
def geo(tmp_path, monkeypatch):
    clock = _Clock(T0)
    monkeypatch.setattr(geocode, "GEOCACHE", tmp_path / "geocache.json")
    monkeypatch.setattr(geocode, "_now", clock)
    monkeypatch.setattr(geocode.time, "sleep", lambda _: None)
    monkeypatch.setattr(geocode.httpx, "Client", lambda **kw: _NullClient())
    calls: list[tuple[str, str]] = []

    def fake_google(client, key, query):
        calls.append(("google", query))
        return 51.5, -0.1

    def fake_nominatim(client, query, email):
        calls.append(("nominatim", query))
        return 51.6, -0.2

    monkeypatch.setattr(geocode, "_google", fake_google)
    monkeypatch.setattr(geocode, "_nominatim", fake_nominatim)
    return clock, calls


def test_google_geocode_is_served_from_cache_within_30_days(geo):
    clock, calls = geo
    s = Settings(google_geocoding_key="k")
    geocode.geocode_place("Tel Aviv", s)
    clock.t = T0 + 29 * DAY
    assert geocode.geocode_place("Tel Aviv", s) == (51.5, -0.1)
    assert len(calls) == 1


def test_google_geocode_expires_after_30_days(geo):
    clock, calls = geo
    s = Settings(google_geocoding_key="k")
    geocode.geocode_place("Tel Aviv", s)
    clock.t = T0 + 31 * DAY
    geocode.geocode_place("Tel Aviv", s)
    assert len(calls) == 2


def test_expired_google_entry_is_dropped_from_disk(geo):
    clock, _ = geo
    geocode.geocode_place("Tel Aviv", Settings(google_geocoding_key="k"))
    clock.t = T0 + 31 * DAY
    geocode.geocode_place("Haifa", Settings(nominatim_email="me@example.org"))
    on_disk = json.loads(geocode.GEOCACHE.read_text(encoding="utf-8"))
    assert set(on_disk) == {"Haifa"}


def test_nominatim_geocode_persists_indefinitely(geo):
    clock, calls = geo
    s = Settings(nominatim_email="me@example.org")
    geocode.geocode_place("Haifa", s)
    clock.t = T0 + 3650 * DAY
    assert geocode.geocode_place("Haifa", s) == (51.6, -0.2)
    assert len(calls) == 1


def test_legacy_entry_without_provenance_is_refetched(geo):
    """`[lat, lng]` from before 0.2 may be Google's; its age is unknown."""
    _, calls = geo
    geocode.GEOCACHE.write_text(json.dumps({"Haifa": [1.0, 2.0]}), encoding="utf-8")
    assert geocode.geocode_place("Haifa", Settings(nominatim_email="x@y.z")) == (
        51.6, -0.2)
    assert len(calls) == 1


def test_geocode_missing_expires_google_entries_too(geo):
    clock, calls = geo
    s = Settings(google_geocoding_key="k")
    geocode.geocode_missing([_venue()], s)
    clock.t = T0 + 10 * DAY
    geocode.geocode_missing([_venue()], s)
    assert len(calls) == 1
    clock.t = T0 + 31 * DAY
    out = geocode.geocode_missing([_venue()], s)
    assert len(calls) == 2
    assert out[0].has_coords


def test_unreadable_geocache_starts_empty(geo):
    geocode.GEOCACHE.write_text("{not json", encoding="utf-8")
    assert geocode.geocode_place("Haifa", Settings(nominatim_email="x@y.z"))


# --- places --------------------------------------------------------------

@pytest.fixture
def plc(tmp_path, monkeypatch):
    clock = _Clock(T0)
    monkeypatch.setattr(places, "PLACES_CACHE", tmp_path / "places_cache.json")
    monkeypatch.setattr(places, "_now", clock)
    monkeypatch.setattr(places.httpx, "Client", lambda **kw: _NullClient())
    calls: list[str] = []

    def fake_search(client, key, query):
        calls.append(query)
        return PlaceMatch(place_id="pid-1", display_name="Ghost Whale",
                          business_status="OPERATIONAL", types=("bar",))

    monkeypatch.setattr(places, "_search", fake_search)
    return clock, calls


def _keyed() -> Settings:
    return Settings(google_places_key="k")


def test_places_status_is_served_from_cache_within_30_days(plc):
    clock, calls = plc
    places.resolve_closures([_venue()], _keyed())
    clock.t = T0 + 29 * DAY
    out = places.resolve_closures([_venue()], _keyed())
    assert out[0].business_status == "OPERATIONAL"
    assert len(calls) == 1


def test_places_status_expires_after_30_days(plc):
    clock, calls = plc
    places.resolve_closures([_venue()], _keyed())
    clock.t = T0 + 31 * DAY
    out = places.resolve_closures([_venue()], _keyed())
    assert out[0].business_status == "OPERATIONAL"
    assert len(calls) == 2


def test_stale_places_entry_keeps_only_the_place_id_on_disk(plc):
    clock, _ = plc
    places.resolve_closures([_venue()], _keyed())
    clock.t = T0 + 31 * DAY
    # Loading and saving with nothing to ask is enough to prune.
    places._save_cache(places._load_cache())
    on_disk = json.loads(places.PLACES_CACHE.read_text(encoding="utf-8"))
    assert list(on_disk.values()) == [{"place_id": "pid-1"}]


def test_places_entry_without_timestamp_is_not_served(plc):
    """Written before the expiry existed: age unknown, so not fresh."""
    _, calls = plc
    query = places._query_for(_venue())
    places.PLACES_CACHE.write_text(json.dumps({query: {
        "place_id": "old", "display_name": "X",
        "business_status": "CLOSED_PERMANENTLY", "types": ["bar"],
    }}), encoding="utf-8")
    out = places.resolve_closures([_venue()], _keyed())
    assert out[0].business_status == "OPERATIONAL"
    assert len(calls) == 1
