"""OpenStreetMap answers "what drinking places are within N km of here".

Untappd's own search cannot: it matches venue *names*, so `q="Tel Aviv"`
returns a corpus of train stations, hotels and a pita place that happen to
carry the city in their name, and misses a bar two streets from the centre
called `Lauter`. Measured on 2026-09-20: one Overpass query returned 258
named drinking venues within 15 km of Tel Aviv, every one with coordinates.

Overpass is preferred over `places.nearby_venue_names` for the same slot
because it needs no key, bills nobody, and keeps Google out of the
*collection* path rather than just the enrichment one.

Two rules this module exists to hold:

- **A broken integration is not an empty city.** An HTTP failure raises
  `OverpassUnavailable`; it never returns `[]`. `[]` means OSM genuinely maps
  no drinking venues there, which is a real answer about a real place.
- **Nearest first.** OSM has no notion of prominence to sort by, so when a
  limit truncates the list it must keep the venues closest to the centre --
  truncating an arbitrary order silently changes which city you asked about.

No network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town import overpass
from beer_in_this_town.config import Settings
from beer_in_this_town.overpass import (
    OverpassUnavailable,
    nearby_venue_names,
    nearby_venues,
)

TLV = (32.0853, 34.7818)


class _Resp:
    def __init__(self, code: int, payload: dict | None = None, text: str = ""):
        self.status_code = code
        self._p = payload if payload is not None else {}
        self.text = text or str(payload)

    def json(self):
        return self._p


class _Client:
    """Records the last request and replays a queued response."""

    response = _Resp(200, {"elements": []})
    last_body = ""

    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    calls = 0

    def post(self, url, content=None, headers=None, **kw):
        type(self).last_body = content or ""
        type(self).calls += 1
        return type(self).response


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(overpass.httpx, "Client", _Client)
    # Each test gets its own cache. Sharing one would let an earlier test's
    # answer be served to a later one -- and the module's whole job is not
    # serving a stale answer as a fresh fact.
    monkeypatch.setattr(overpass, "OVERPASS_CACHE", tmp_path / "overpass.json")
    monkeypatch.setattr(overpass, "STATE_DIR", tmp_path)
    _Client.response = _Resp(200, {"elements": []})
    _Client.last_body = ""
    _Client.calls = 0
    return _Client


def _node(name, lat, lng, **tags):
    return {"type": "node", "id": 1, "lat": lat, "lon": lng,
            "tags": {"name": name, "amenity": "bar", **tags}}


def _way(name, lat, lng):
    return {"type": "way", "id": 2, "center": {"lat": lat, "lon": lng},
            "tags": {"name": name, "amenity": "pub"}}


# --- the query ------------------------------------------------------------

def test_query_carries_the_radius_in_metres_and_the_centre(offline):
    nearby_venues(TLV, 15.0, Settings())
    body = offline.last_body.decode("utf-8")
    assert "around:15000" in body
    assert "32.0853" in body and "34.7818" in body


def test_radius_is_explicit_and_has_no_default(offline):
    """Same reasoning as the city: a default radius answers a question only
    the user can answer, and answers it about somewhere they never named."""
    with pytest.raises(TypeError):
        nearby_venues(TLV, Settings())  # type: ignore[call-arg]


def test_needs_no_api_key(offline):
    """The whole point of preferring this over Places. Default Settings carry
    no key of any kind and the call must still work."""
    assert Settings().google_places_key is None
    nearby_venues(TLV, 5.0, Settings())


# --- parsing --------------------------------------------------------------

def test_reads_nodes_and_ways_alike(offline):
    offline.response = _Resp(200, {"elements": [
        _node("Lauter", 32.0703, 34.7846),
        _way("Beer Bazaar", 32.0704, 34.7847),
    ]})
    got = nearby_venues(TLV, 15.0, Settings())
    # Order is distance's business, asserted in `test_nearest_first`; what
    # this test is about is that a way is read at all, via `center`.
    assert {v.name for v in got} == {"Lauter", "Beer Bazaar"}
    assert all(v.lat and v.lng for v in got)


def test_an_element_with_no_name_is_dropped(offline):
    """A venue with no name is not a search term and cannot become one."""
    offline.response = _Resp(200, {"elements": [
        _node("Lauter", 32.0703, 34.7846),
        {"type": "node", "id": 3, "lat": 32.07, "lon": 34.78,
         "tags": {"amenity": "bar"}},
    ]})
    assert [v.name for v in nearby_venues(TLV, 15.0, Settings())] == ["Lauter"]


def test_an_element_with_no_coordinates_is_dropped(offline):
    """Coordinates are what make the join checkable later. A nameless point
    and a placeless name are both unusable, for the same reason."""
    offline.response = _Resp(200, {"elements": [
        {"type": "way", "id": 4, "tags": {"name": "Ghost", "amenity": "bar"}},
    ]})
    assert nearby_venues(TLV, 15.0, Settings()) == []


def test_kind_comes_from_whichever_tag_carried_it(offline):
    offline.response = _Resp(200, {"elements": [
        _node("Shop", 32.07, 34.78, amenity=None, shop="alcohol"),
    ]})
    assert nearby_venues(TLV, 15.0, Settings())[0].kind == "alcohol"


# --- ordering and truncation ---------------------------------------------

def test_nearest_first(offline):
    offline.response = _Resp(200, {"elements": [
        _node("Far", 32.20, 34.90),
        _node("Near", 32.0854, 34.7819),
        _node("Middle", 32.12, 34.82),
    ]})
    assert [v.name for v in nearby_venues(TLV, 25.0, Settings())] == [
        "Near", "Middle", "Far"]


def test_a_limit_keeps_the_nearest_not_the_first_seen(offline):
    offline.response = _Resp(200, {"elements": [
        _node("Far", 32.20, 34.90),
        _node("Near", 32.0854, 34.7819),
    ]})
    assert nearby_venue_names(TLV, 25.0, Settings(), limit=1) == ["Near"]


def test_names_are_deduplicated_but_venues_are_not(offline):
    """Two bars really can share a name; as *search terms* the duplicate is
    a wasted request, as *venues* it is two different places."""
    offline.response = _Resp(200, {"elements": [
        _node("Mike's Place", 32.086, 34.782),
        _node("Mike's Place", 32.09, 34.79),
    ]})
    assert len(nearby_venues(TLV, 25.0, Settings())) == 2
    assert nearby_venue_names(TLV, 25.0, Settings()) == ["Mike's Place"]


# --- failure semantics ----------------------------------------------------

def test_empty_is_a_real_answer_not_an_error(offline):
    """OSM maps no bars there. That is a fact about the place, and the
    caller must be able to tell it from a broken integration."""
    offline.response = _Resp(200, {"elements": []})
    assert nearby_venues(TLV, 5.0, Settings()) == []


@pytest.mark.parametrize("code", [429, 504, 500, 403])
def test_a_refusal_raises_rather_than_reporting_an_empty_city(offline, code):
    offline.response = _Resp(code, {}, text="rate_limited")
    with pytest.raises(OverpassUnavailable):
        nearby_venues(TLV, 5.0, Settings())


def test_unparseable_body_raises_too(offline):
    class _Bad(_Resp):
        def json(self):
            raise ValueError("not json")

    offline.response = _Bad(200, {}, text="<html>maintenance</html>")
    with pytest.raises(OverpassUnavailable):
        nearby_venues(TLV, 5.0, Settings())


def test_transport_failure_raises_overpass_unavailable(offline, monkeypatch):
    def _boom(*a, **kw):
        raise overpass.httpx.ConnectError("no route")

    monkeypatch.setattr(_Client, "post", _boom)
    with pytest.raises(OverpassUnavailable):
        nearby_venues(TLV, 5.0, Settings())


# --- caching --------------------------------------------------------------

def test_the_same_question_is_asked_once(offline):
    """Overpass is volunteer-run and asks callers to cache. The first live
    run of this module hit 504 on its *second* request for data it had
    fetched seconds earlier, because `nearby_venue_names` re-asked."""
    offline.response = _Resp(200, {"elements": [_node("Lauter", 32.07, 34.78)]})
    nearby_venues(TLV, 15.0, Settings())
    nearby_venue_names(TLV, 15.0, Settings())
    assert offline.calls == 1


def test_a_different_radius_is_a_different_question(offline):
    offline.response = _Resp(200, {"elements": [_node("Lauter", 32.07, 34.78)]})
    nearby_venues(TLV, 15.0, Settings())
    nearby_venues(TLV, 5.0, Settings())
    assert offline.calls == 2


def test_a_stale_entry_is_refetched(offline):
    offline.response = _Resp(200, {"elements": [_node("Lauter", 32.07, 34.78)]})
    nearby_venues(TLV, 15.0, Settings())
    nearby_venues(TLV, 15.0, Settings(cache_ttl_s=0))
    assert offline.calls == 2


def test_a_failure_is_never_cached(offline):
    """A 504 that cached itself as "no bars here" would survive for twelve
    hours and read exactly like a city with no pubs in it."""
    offline.response = _Resp(504, {}, text="too busy")
    with pytest.raises(OverpassUnavailable):
        nearby_venues(TLV, 15.0, Settings())

    offline.response = _Resp(200, {"elements": [_node("Lauter", 32.07, 34.78)]})
    assert [v.name for v in nearby_venues(TLV, 15.0, Settings())] == ["Lauter"]


# --- licence --------------------------------------------------------------

def test_attribution_is_carried_in_the_module(offline):
    """ODbL. Data lifted into an exported CSV or KML carries an attribution
    obligation, so the string lives next to the code that fetches it rather
    than in somebody's memory."""
    assert "OpenStreetMap" in overpass.ATTRIBUTION
