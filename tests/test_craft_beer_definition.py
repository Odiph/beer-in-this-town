"""The one definition of a craft-beer venue, and the small fixes around it.

`filter` and the app's category panel read the same vocabulary; these tests
pin the decisions the v0.2 contract made about the contested categories.
"""
from __future__ import annotations

import pytest

from beer_in_this_town import http_client
from beer_in_this_town.app_categories import DRINKING_CATEGORIES
from beer_in_this_town.classify import (
    EXCLUDED_CATEGORIES,
    KEEP_CATEGORIES,
    craft_beer_decision,
)
from beer_in_this_town.config import Settings
from beer_in_this_town.models import Venue, VenueRef

pytestmark = pytest.mark.unit


def v(category, total=500, unique=300, monthly=20, status=""):
    return Venue(ref=VenueRef(venue_id="1", slug="x", name="X",
                              category=category, address=None, city="TLV"),
                 total=total, unique=unique, monthly=monthly, you=None,
                 business_status=status)


@pytest.mark.parametrize("category,kind", [
    ("Brewery", "brewery"), ("Brewpub", "brewery"), ("Taproom", "brewery"),
    ("Beer Bar", "beer_bar"), ("Beer Garden", "beer_bar"),
    ("Bottle Shop", "bottle_shop"), ("Beer Store, Hobby Shop", "bottle_shop"),
    ("Pub", "bar"), ("Bar", "bar"), ("Irish Pub", "bar"), ("Hotel Bar", "bar"),
    ("American Restaurant, Beer Bar, Dive Bar, Bar", "beer_bar"),
    ("Wine Bar, Beer Bar", "beer_bar"),   # a beer signal wins
    ("Restaurant, Bar", "bar"),
])
def test_kept(category, kind):
    d = craft_beer_decision(v(category))
    assert d.keep and d.kind == kind


@pytest.mark.parametrize("category,reason", [
    ("Supermarket", "supermarket"), ("Gas Station", "gas station"),
    ("Highway or Road", "highway/road"), ("Winery", "winery"),
    ("Wine Bar", "wine bar"), ("Cocktail Bar", "cocktail bar"),
    ("Wine Bar, Bar", "wine bar"),        # exclusion beats a generic bar
    ("Italian Restaurant, Pizza Place", "restaurant-only"),
    ("Hotel", "no beer category: Hotel"),
])
def test_excluded_with_a_reason(category, reason):
    d = craft_beer_decision(v(category))
    assert not d.keep and d.reason == reason


def test_no_category_is_kept_as_uncategorised():
    d = craft_beer_decision(v(None, total=None, unique=None, monthly=None))
    assert d.keep and d.kind == "uncategorised"


def test_private_is_excluded_before_its_category_can_keep_it():
    d = craft_beer_decision(v("Brewery", total=300, unique=2))
    assert not d.keep and d.reason.startswith("private")


def test_closed_is_flagged_never_dropped():
    quiet = craft_beer_decision(v("Pub", total=500, monthly=0))
    assert quiet.keep and quiet.flag == "possibly_closed"
    shut = craft_beer_decision(v("Pub", status="CLOSED_PERMANENTLY"))
    assert shut.keep and shut.flag == "closed"


def test_accented_categories_match():
    assert craft_beer_decision(v("Brasserie, Café, BAR")).keep


def test_the_app_panel_and_the_filter_share_one_vocabulary():
    assert DRINKING_CATEGORIES == KEEP_CATEGORIES
    assert not set(EXCLUDED_CATEGORIES) & KEEP_CATEGORIES


# --- models round trip -----------------------------------------------------

def test_from_row_inverts_to_row():
    original = Venue(ref=VenueRef(venue_id="7", slug="lauter", name="Lauter",
                                  category="Bar", address="10 St",
                                  city="Tel Aviv"),
                     total=5, unique=None, monthly=0, you=None,
                     lat=32.07, lng=34.78, geo_source="embedded",
                     business_status="OPERATIONAL")
    back = Venue.from_row({k: str(x) for k, x in original.to_row().items()})
    assert back == original
    assert back.ref.url == "https://untappd.com/v/lauter/7"


def test_from_row_keeps_unknown_unknown():
    back = Venue.from_row({"name": "Nowhere", "total": "", "venue_id": ""})
    assert back.total is None and back.ref.venue_id == "" and back.ref.url == ""


# --- PoliteClient: a paid response survives a failed cache write -----------

class _Resp:
    status_code = 200
    text = "<html>paid for</html>"
    headers: dict = {}

    def raise_for_status(self):
        pass


def test_cache_write_failure_does_not_lose_a_paid_response(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(http_client, "CACHE_DIR", blocker / "cache")
    with http_client.PoliteClient(Settings()) as client:
        monkeypatch.setattr(client, "_throttle", lambda: None)
        calls = []
        monkeypatch.setattr(client._client, "get",
                            lambda *a, **k: calls.append(1) or _Resp())
        assert client.get("https://untappd.com/v/x/1") == _Resp.text
    assert calls == [1]  # one request, no retry spent on the cache failure


def test_cache_directory_is_created_on_demand(tmp_path, monkeypatch):
    monkeypatch.setattr(http_client, "CACHE_DIR", tmp_path / "fresh" / "cache")
    with http_client.PoliteClient(Settings()) as client:
        monkeypatch.setattr(client, "_throttle", lambda: None)
        monkeypatch.setattr(client._client, "get", lambda *a, **k: _Resp())
        client.get("https://untappd.com/v/x/1")
    assert list((tmp_path / "fresh" / "cache").iterdir())
