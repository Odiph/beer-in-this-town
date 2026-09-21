"""Nominatim asks every caller for a contact; a missing one is said out loud.

The old fallback sent the literal string "no-contact-set" and told nobody.
Now: it still works without NOMINATIM_EMAIL, but warns once per process, and
the User-Agent names the project and its version instead. No network.
"""
from __future__ import annotations

import logging

import pytest

from beer_in_this_town import __version__, geocode, overpass
from beer_in_this_town.config import Settings

pytestmark = pytest.mark.unit


class _RecordingClient:
    headers_seen: list[dict] = []

    def __init__(self, **kw) -> None:
        _RecordingClient.headers_seen.append(kw.get("headers") or {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(geocode, "GEOCACHE", tmp_path / "geocache.json")
    monkeypatch.setattr(geocode, "_warned_no_contact", False)
    monkeypatch.setattr(geocode.time, "sleep", lambda _: None)
    _RecordingClient.headers_seen = []
    monkeypatch.setattr(geocode.httpx, "Client", _RecordingClient)
    monkeypatch.setattr(geocode, "_nominatim", lambda c, q, e: (1.0, 2.0))


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "NOMINATIM_EMAIL" in r.getMessage()]


def test_no_contact_still_geocodes_and_warns_once(offline, caplog):
    caplog.set_level(logging.WARNING, logger=geocode.__name__)
    s = Settings()
    assert geocode.geocode_place("Haifa", s) == (1.0, 2.0)
    assert geocode.geocode_place("Acre", s) == (1.0, 2.0)
    assert len(_warnings(caplog)) == 1


def test_no_contact_is_never_sent_as_a_placeholder(offline):
    geocode.geocode_place("Haifa", Settings())
    ua = _RecordingClient.headers_seen[0]["User-Agent"]
    assert "no-contact-set" not in ua
    assert f"beer-in-this-town/{__version__}" in ua
    assert "github.com/Odiph/beer-in-this-town" in ua


def test_a_contact_is_sent_and_nothing_is_warned(offline, caplog):
    caplog.set_level(logging.WARNING, logger=geocode.__name__)
    geocode.geocode_place("Haifa", Settings(nominatim_email="me@example.org"))
    assert "me@example.org" in _RecordingClient.headers_seen[0]["User-Agent"]
    assert _warnings(caplog) == []


def test_google_geocoding_does_not_nag_about_nominatim(offline, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=geocode.__name__)
    monkeypatch.setattr(geocode, "_google", lambda c, k, q: (1.0, 2.0))
    geocode.geocode_place("Haifa", Settings(google_geocoding_key="k"))
    assert _warnings(caplog) == []


def test_a_cache_hit_does_not_warn(offline, caplog):
    geocode.geocode_place("Haifa", Settings(nominatim_email="me@example.org"))
    caplog.set_level(logging.WARNING, logger=geocode.__name__)
    geocode.geocode_place("Haifa", Settings())
    assert _warnings(caplog) == []


def test_user_agents_share_one_version():
    assert f"/{__version__} " in overpass.USER_AGENT
    assert geocode._user_agent(Settings()).startswith(
        f"beer-in-this-town/{__version__} ")
