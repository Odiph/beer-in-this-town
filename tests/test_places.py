"""A venue Places cannot find is unknown, never closed.

That is the load-bearing rule from #7 and #20, and it is the one an
implementation drifts away from by accident: the natural shape is a per-venue
loop, and a per-venue loop wants to treat "no answer" as an answer. Every
failure here -- no match, a timeout, a renamed venue -- must leave the venue
visible, because a false closure silently deletes a real bar from the map.

The second half of the file is this repo's own recurring failure mode, applied
to a new module before it can happen again: `PlacesUnavailable` is raised where
a key or a quota is wrong, and it is tested at the *caller*, because the last
five times this went wrong the raise was in the diff and a broad `except` two
frames up swallowed it.

No network.
"""
from __future__ import annotations

import json

import pytest

from beer_in_this_town import places
from beer_in_this_town.config import Settings
from beer_in_this_town.models import Venue, VenueRef
from beer_in_this_town.places import (
    PlaceMatch,
    PlacesUnavailable,
    resolve_closures,
)


def _venue(vid: str, name: str, city: str = "London") -> Venue:
    return Venue(
        ref=VenueRef(venue_id=vid, slug=name.lower().replace(" ", "-"), name=name,
                     category="Beer Bar", address=f"{vid} Some Road", city=city),
        total=400, unique=90, monthly=0, you=None,
    )


def _match(status: str | None, types: tuple[str, ...] = ("bar",)) -> PlaceMatch:
    return PlaceMatch(place_id="pid", display_name="X",
                      business_status=status, types=types)


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """No cache carried in, no HTTP client, no sleeping."""
    monkeypatch.setattr(places, "PLACES_CACHE", tmp_path / "places_cache.json")
    monkeypatch.setattr(places.httpx, "Client", lambda **kw: _NullClient())


class _NullClient:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _keyed() -> Settings:
    return Settings(google_places_key="test-key")


# --- the rule: unresolved is not closed ----------------------------------

@pytest.mark.unit
def test_no_places_match_leaves_the_venue_visible(offline, monkeypatch):
    """The place closed, was renamed, or was never listed. Do not pick one."""
    monkeypatch.setattr(places, "_search", lambda c, k, q: None)
    out = resolve_closures([_venue("1", "Ghost Whale")], _keyed())
    assert out[0].business_status == places.UNMATCHED
    assert out[0].is_closed is False


@pytest.mark.unit
def test_a_single_venue_blip_does_not_condemn_it(offline, monkeypatch):
    """One timeout is a per-venue fact. It must not read as a closure."""
    def blip_once(client, key, query):
        if query.startswith("Ghost Whale"):
            raise RuntimeError("read timeout")
        return _match("OPERATIONAL")

    monkeypatch.setattr(places, "_search", blip_once)
    # A second venue that answers keeps the all-failed guard from firing, so
    # what is under test here is the one blip and not the abort.
    out = resolve_closures([_venue("1", "Ghost Whale"), _venue("2", "Kill The Cat")],
                           _keyed())
    assert out[0].business_status == places.UNCHECKED
    assert out[0].is_closed is False
    assert out[1].business_status == "OPERATIONAL"


@pytest.mark.unit
@pytest.mark.parametrize("status,closed", [
    ("CLOSED_PERMANENTLY", True),
    ("CLOSED_TEMPORARILY", True),
    ("OPERATIONAL", False),
    ("FUTURE_OPENING", False),
    (None, False),
])
def test_only_an_explicit_closed_status_closes_a_venue(offline, monkeypatch,
                                                       status, closed):
    """FUTURE_OPENING is the trap: a real status, and the venue is not shut."""
    monkeypatch.setattr(places, "_search", lambda c, k, q: _match(status))
    out = resolve_closures([_venue("1", "Ghost Whale")], _keyed())
    assert out[0].is_closed is closed


@pytest.mark.unit
def test_a_status_google_has_not_invented_yet_is_not_a_closure(offline, monkeypatch):
    """An unknown enum value must fail open, not guess."""
    monkeypatch.setattr(places, "_search", lambda c, k, q: _match("SEASONAL_MAYBE"))
    out = resolve_closures([_venue("1", "Ghost Whale")], _keyed())
    assert out[0].is_closed is False


# --- the recurring failure mode, tested at the caller ---------------------

@pytest.mark.unit
def test_a_rejected_key_aborts_rather_than_marking_everything_unmatched(
        offline, monkeypatch):
    """`_search` raises; the question is whether its caller swallows it.

    This is the fifth time this repo has written that raise and the first
    four were caught by a broad `except` one frame up. If this ever fails,
    a bad key ships a CSV of `unmatched` and reports success.
    """
    def denied(client, key, query):
        raise PlacesUnavailable("REQUEST_DENIED: Places API not enabled")

    monkeypatch.setattr(places, "_search", denied)
    with pytest.raises(PlacesUnavailable):
        resolve_closures([_venue("1", "Ghost Whale")], _keyed())


@pytest.mark.unit
def test_every_lookup_failing_is_a_broken_integration_not_bad_luck(
        offline, monkeypatch):
    """Mirrors geocode's guard: a hundred blips in a row is not a hundred blips."""
    def blip(client, key, query):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(places, "_search", blip)
    with pytest.raises(PlacesUnavailable):
        resolve_closures([_venue(str(i), f"Bar {i}") for i in range(5)], _keyed())


@pytest.mark.unit
def test_no_key_is_a_no_op_and_never_a_verdict(offline):
    """Without a key the stage declines to answer. It does not guess."""
    out = resolve_closures([_venue("1", "Ghost Whale")], Settings())
    assert out[0].business_status == places.UNCHECKED
    assert out[0].is_closed is False


# --- the request Google actually receives --------------------------------

@pytest.mark.unit
def test_the_field_mask_asks_for_business_status(monkeypatch):
    """Without this field in the mask the response has no status at all.

    Asserted on the outgoing request rather than on a parsed fixture,
    because a mask that omits the field parses perfectly into "unmatched".
    """
    sent: dict = {}

    class _Client:
        def post(self, url, headers=None, json=None, **kw):
            sent.update(url=url, headers=headers, json=json)
            return _Response(200, {"places": [{"id": "p", "displayName":
                                               {"text": "X"},
                                               "businessStatus": "OPERATIONAL"}]})

    match = places._search(_Client(), "key", "Ghost Whale, London")
    assert "places.businessStatus" in sent["headers"]["X-Goog-FieldMask"]
    assert sent["headers"]["X-Goog-Api-Key"] == "key"
    assert sent["url"] == places.SEARCH_TEXT_URL
    assert sent["json"]["textQuery"] == "Ghost Whale, London"
    assert match.business_status == "OPERATIONAL"


@pytest.mark.unit
def test_an_empty_places_array_is_no_match_not_an_error():
    class _Client:
        def post(self, *a, **kw):
            return _Response(200, {})

    assert places._search(_Client(), "key", "Nowhere") is None


@pytest.mark.unit
@pytest.mark.parametrize("code", [401, 403, 429])
def test_key_and_quota_failures_raise(code):
    """Config and billing problems are never per-venue."""
    class _Client:
        def post(self, *a, **kw):
            return _Response(code, {"error": {"message": "nope"}})

    with pytest.raises(PlacesUnavailable):
        places._search(_Client(), "key", "Ghost Whale")


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


# --- cache ----------------------------------------------------------------

@pytest.mark.unit
def test_a_resolved_venue_is_not_billed_twice(offline, monkeypatch):
    """The 12h scrape cache exists for this reason; so does this one."""
    calls = []

    def once(client, key, query):
        calls.append(query)
        return _match("CLOSED_PERMANENTLY")

    monkeypatch.setattr(places, "_search", once)
    venues = [_venue("1", "Ghost Whale")]
    first = resolve_closures(venues, _keyed())
    second = resolve_closures(venues, _keyed())

    assert len(calls) == 1
    assert first[0].business_status == second[0].business_status == "CLOSED_PERMANENTLY"


@pytest.mark.unit
def test_an_unmatched_venue_is_retried_on_the_next_run(offline, monkeypatch):
    """A venue Places did not know last week may be listed this week.

    Caching a negative would make "not found once" permanent, which is the
    unresolved-is-not-a-verdict rule leaking into the cache.
    """
    calls = []

    def missing(client, key, query):
        calls.append(query)
        return None

    monkeypatch.setattr(places, "_search", missing)
    venues = [_venue("1", "Ghost Whale")]
    resolve_closures(venues, _keyed())
    resolve_closures(venues, _keyed())
    assert len(calls) == 2


# --- the row that reaches the CSV ----------------------------------------

@pytest.mark.unit
def test_business_status_survives_a_csv_round_trip(tmp_path, offline, monkeypatch):
    """A column nothing reads back is a column that does not exist."""
    from beer_in_this_town.export import write_csv
    from beer_in_this_town.measure import venues_from_csv

    monkeypatch.setattr(places, "_search", lambda c, k, q: _match("CLOSED_PERMANENTLY"))
    resolved = resolve_closures([_venue("1", "Ghost Whale")], _keyed())

    path = tmp_path / "venues.csv"
    write_csv(resolved, path)
    restored = venues_from_csv(path)

    assert restored[0].business_status == "CLOSED_PERMANENTLY"
    assert restored[0].is_closed is True


@pytest.mark.unit
def test_a_csv_written_before_this_column_existed_still_loads(tmp_path):
    """Old CSVs are unchecked, which is exactly what they are."""
    from beer_in_this_town.measure import venues_from_csv

    path = tmp_path / "old.csv"
    path.write_text("venue_id,name,total,unique,monthly\n1,Ghost Whale,400,90,0\n",
                    encoding="utf-8")
    v = venues_from_csv(path)[0]
    assert v.business_status == places.UNCHECKED
    assert v.is_closed is False


# --- the cache as a liability --------------------------------------------

@pytest.mark.unit
def test_a_corrupt_cache_costs_money_not_the_run(offline, monkeypatch, caplog):
    """Half a JSON file must not abort a run. Start empty and say so."""
    places.PLACES_CACHE.write_text("{not json at all", encoding="utf-8")
    monkeypatch.setattr(places, "_search", lambda c, k, q: _match("OPERATIONAL"))

    out = resolve_closures([_venue("1", "Ghost Whale")], _keyed())
    assert out[0].business_status == "OPERATIONAL"
    assert "re-bill" in caplog.text


@pytest.mark.unit
def test_lookups_paid_for_before_an_abort_are_still_cached(offline, monkeypatch):
    """A key that dies halfway must not make the first half billable twice."""
    def dies_on_the_third(client, key, query):
        if query.startswith("Bar 3"):
            raise PlacesUnavailable("HTTP 429: quota exceeded")
        return _match("OPERATIONAL")

    monkeypatch.setattr(places, "_search", dies_on_the_third)
    venues = [_venue(str(i), f"Bar {i}") for i in range(1, 5)]
    with pytest.raises(PlacesUnavailable):
        resolve_closures(venues, _keyed())

    cached = json.loads(places.PLACES_CACHE.read_text(encoding="utf-8"))
    assert len(cached) == 2, "the two successful lookups were thrown away"
