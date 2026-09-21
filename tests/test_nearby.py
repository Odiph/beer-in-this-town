"""Untappd's search finds venues whose *name* matches. Google knows what is there.

Searching "Tel Aviv" returned a grill in Encino and missed bars two streets
from the centre, because a bar called "Lauter" has no reason to carry the city
in its name. Asking Google what is actually within a radius is the other half
of that problem -- and the names it returns become the search terms.

Places cannot be asked for neighbourhoods: `locality` and `neighborhood` are
Table B types, returned in responses and refused as filters. Asking for the
bars themselves is the request that exists.

No network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town import places
from beer_in_this_town.config import Settings
from beer_in_this_town.places import PlacesUnavailable, nearby_venue_names

TLV = (32.0853, 34.7818)


def _keyed() -> Settings:
    return Settings(google_places_key="test-key")


class _Resp:
    def __init__(self, code, payload):
        self.status_code, self._p, self.text = code, payload, str(payload)

    def json(self):
        return self._p


def _named(*names):
    return {"places": [{"id": n, "displayName": {"text": n}} for n in names]}


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(places.httpx, "Client", lambda **kw: _Client())


class _Client:
    sent: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None, **kw):
        _Client.sent.append(json)
        return _Resp(200, _named("Lauter", "Schnitt Brewing Company"))


@pytest.mark.unit
def test_no_key_means_no_names(monkeypatch):
    """Same as everywhere else: absent is a no-op, never a guess."""
    assert nearby_venue_names(TLV, 15, Settings()) == []


@pytest.mark.unit
def test_names_come_back_deduplicated(offline):
    _Client.sent = []
    names = nearby_venue_names(TLV, 15, _keyed())
    assert names == ["Lauter", "Schnitt Brewing Company"], names


@pytest.mark.unit
def test_the_request_is_a_circle_around_the_centre(offline):
    _Client.sent = []
    nearby_venue_names(TLV, 15, _keyed())
    body = _Client.sent[0]
    circle = body["locationRestriction"]["circle"]

    assert circle["center"] == {"latitude": TLV[0], "longitude": TLV[1]}
    assert circle["radius"] == 15_000
    assert body["includedTypes"], "no type filter: this would return anything"


@pytest.mark.unit
def test_only_table_a_types_are_asked_for():
    """`locality` and `neighborhood` are Table B. Google refuses them as a
    filter, so a request carrying one fails rather than returning suburbs."""
    for forbidden in ("locality", "neighborhood", "sublocality",
                      "administrative_area_level_1"):
        assert forbidden not in places.NEARBY_TYPES


@pytest.mark.unit
def test_googles_own_radius_ceiling_is_respected(offline, caplog):
    _Client.sent = []
    nearby_venue_names(TLV, 200, _keyed())
    circle = _Client.sent[0]["locationRestriction"]["circle"]

    assert circle["radius"] == places.MAX_NEARBY_RADIUS_M
    assert "capped" in caplog.text, "the cap was applied without saying so"


@pytest.mark.unit
def test_a_dead_key_stops_everything(monkeypatch):
    """Not per-type: the next call fails the same way. Same rule as closures."""
    class _Denied(_Client):
        def post(self, *a, **kw):
            return _Resp(403, {"error": {"message": "API not enabled"}})

    monkeypatch.setattr(places.httpx, "Client", lambda **kw: _Denied())
    with pytest.raises(PlacesUnavailable):
        nearby_venue_names(TLV, 15, _keyed())


@pytest.mark.unit
def test_one_type_failing_does_not_lose_the_others(monkeypatch):
    """A 500 on `night_club` is not a reason to return nothing."""
    class _Flaky(_Client):
        def post(self, url, headers=None, json=None, **kw):
            if json["includedTypes"] == ["bar"]:
                return _Resp(500, {})
            return _Resp(200, _named("Schnitt Brewing Company"))

    monkeypatch.setattr(places.httpx, "Client", lambda **kw: _Flaky())
    assert nearby_venue_names(TLV, 15, _keyed()) == ["Schnitt Brewing Company"]


@pytest.mark.unit
def test_the_field_mask_stays_on_the_cheap_tier():
    """id, displayName and types are Essentials. `location` is Pro, and
    Untappd already supplies coordinates for everything it returns."""
    assert "places.location" not in places.NEARBY_FIELD_MASK
    assert "places.displayName" in places.NEARBY_FIELD_MASK
