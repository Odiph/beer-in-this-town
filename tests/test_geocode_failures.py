"""A geocoder that is not working must say so, not return fewer pins.

`_google` raises on REQUEST_DENIED and OVER_QUERY_LIMIT with a comment saying
to surface them rather than quietly degrade -- and its only caller wrapped
every call in `except Exception: continue`, which is exactly quietly degrading.
A bad key produced a KML with four pins instead of a hundred and `ok: true`.
No network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town import geocode
from beer_in_this_town.config import Settings
from beer_in_this_town.geocode import GeocoderUnavailable, geocode_missing
from beer_in_this_town.models import Venue, VenueRef


def _venue(vid: str, name: str) -> Venue:
    return Venue(
        ref=VenueRef(venue_id=vid, slug=name.lower(), name=name,
                     category=None, address=f"{vid} Some Road", city="London"),
        total=1, unique=1, monthly=1, you=None,
    )


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """No cache, no HTTP, no sleeping."""
    monkeypatch.setattr(geocode, "GEOCACHE", tmp_path / "geocache.json")
    monkeypatch.setattr(geocode.time, "sleep", lambda _: None)
    monkeypatch.setattr(geocode.httpx, "Client", lambda **kw: _NullClient())


class _NullClient:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.unit
def test_a_rejected_key_aborts_rather_than_dropping_pins(offline, monkeypatch):
    """The failure the comment in _google promised to surface."""
    def denied(client, key, query):
        raise GeocoderUnavailable("REQUEST_DENIED: billing not enabled")

    monkeypatch.setattr(geocode, "_google", denied)
    s = Settings(google_geocoding_key="bad-key")
    with pytest.raises(GeocoderUnavailable):
        geocode_missing([_venue("1", "Ghost Whale")], s)


@pytest.mark.unit
def test_a_venue_with_no_match_is_still_skipped_quietly(offline, monkeypatch):
    """ZERO_RESULTS is a per-venue fact, not a broken geocoder."""
    monkeypatch.setattr(geocode, "_nominatim", lambda c, q, e: None)
    out = geocode_missing([_venue("1", "Ghost Whale")], Settings())
    assert out[0].has_coords is False
    assert out[0].geo_source == "none"


@pytest.mark.unit
def test_one_bad_address_does_not_kill_the_run(offline, monkeypatch):
    """A single malformed lookup stays swallowed, as before."""
    calls = {"n": 0}

    def flaky(client, query, email):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("malformed address")
        return (51.5, -0.1)

    monkeypatch.setattr(geocode, "_nominatim", flaky)
    out = geocode_missing([_venue("1", "Bad"), _venue("2", "Good")], Settings())
    assert out[0].has_coords is False
    assert out[1].has_coords is True


@pytest.mark.unit
def test_every_lookup_failing_is_systemic_not_a_hundred_coincidences(
    offline, monkeypatch
):
    """Covers the default path too: Nominatim blocked degrades just as quietly."""
    def always_fails(client, query, email):
        raise OSError("connection refused")

    monkeypatch.setattr(geocode, "_nominatim", always_fails)
    with pytest.raises(GeocoderUnavailable):
        geocode_missing([_venue(str(i), f"V{i}") for i in range(5)], Settings())


@pytest.mark.unit
def test_nothing_to_do_is_never_an_error(offline):
    """No work means no verdict about the geocoder's health."""
    located = _venue("1", "Ghost Whale").with_coords(51.5, -0.1, "embedded")
    assert geocode_missing([located], Settings()) == [located]


@pytest.mark.unit
def test_nominatim_is_paced_even_when_the_lookup_fails(tmp_path, monkeypatch):
    """The pause sat after the call inside the try, so a 429 skipped it.

    Consecutive failures then hit OSM back-to-back -- at exactly the moment
    their usage policy matters most, and the comment citing that policy was
    right above the line that was being skipped.
    """
    slept: list[float] = []
    monkeypatch.setattr(geocode, "GEOCACHE", tmp_path / "geocache.json")
    monkeypatch.setattr(geocode.time, "sleep", slept.append)
    monkeypatch.setattr(geocode.httpx, "Client", lambda **kw: _NullClient())

    calls = {"n": 0}

    def sometimes(client, query, email):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("slow")
        return (51.5, -0.1)

    monkeypatch.setattr(geocode, "_nominatim", sometimes)
    geocode_missing([_venue("1", "Bad"), _venue("2", "Good")], Settings())
    assert len(slept) == 2, "both lookups must be paced, not just the one that worked"
