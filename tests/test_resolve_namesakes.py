"""Namesakes, the real name search, and the fetch that goes with it.

The first live enrich (Tel Aviv, 33 names) lost `Mike's Place`, `Django` and
`Oscar Wilde` the same way: the name search listed far-away namesakes first,
they filled the two-fetch cap, and the local venue was never fetched. The
search card carries a location line; these tests hold the ranking to it.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.app_sweep import Venue as Swept
from beer_in_this_town.config import Settings
from beer_in_this_town.http_client import BudgetExceeded
from beer_in_this_town.models import Venue, VenueRef
from beer_in_this_town.resolve import (
    ELSEWHERE,
    IN_CITY,
    UNKNOWN_PLACE,
    BrowserNameSearch,
    enrich_rows,
    page_fetcher,
    parse_search_cards,
    place_rank,
    resolve_one,
    search_url_for,
)

pytestmark = pytest.mark.unit

TLV = (32.0700, 34.7846)


def carded(name, vid, city):
    return VenueRef(venue_id=vid, slug=name.lower().replace(" ", "-"),
                    name=name, category="Bar", address=None, city=city)


def page(r, lat, lng):
    return Venue(ref=r, total=1000, unique=100, monthly=10, you=None,
                 lat=lat, lng=lng, geo_source="embedded")


class Web:
    def __init__(self, results=None, pages=None):
        self.results = results or {}
        self.pages = pages or {}
        self.searched: list[str] = []
        self.fetched: list[str] = []

    def search(self, q):
        self.searched.append(q)
        return self.results.get(q, [])

    def fetch(self, r):
        self.fetched.append(r.venue_id)
        return self.pages[r.venue_id]


# --- ranking -----------------------------------------------------------------

def test_far_namesakes_do_not_fill_the_fetch_cap():
    far1 = carded("Mike's Place", "1", "Chicago, IL")
    far2 = carded("Mike's Place", "2", "Berlin, Deutschland")
    local = carded("Mike's Place", "3", "Tel Aviv, תל אביב, ישראל")
    name = "Mike's Place (מייקס פלייס)"
    web = Web({name: [far1, far2, local]},
              {"1": page(far1, 41.88, -87.63), "2": page(far2, 52.52, 13.40),
               "3": page(local, 32.0702, 34.7848)})
    out = resolve_one(Swept(name, 0, 0, *TLV), web.search, web.fetch,
                      city="Tel Aviv")
    assert out.status == "resolved" and out.venue.ref.venue_id == "3"
    assert web.fetched == ["3"]
    # A local card was found, so no second search was spent.
    assert web.searched == [name]


def test_a_city_qualified_search_runs_when_no_card_is_local():
    far = carded("Django", "1", "Paris, France")
    local = carded("Django", "2", "Tel Aviv, ישראל")
    web = Web({"Django": [far], "Django Tel Aviv": [local]},
              {"1": page(far, 48.85, 2.35), "2": page(local, 32.0701, 34.7846)})
    out = resolve_one(Swept("Django", 0, 0, *TLV), web.search, web.fetch,
                      city="Tel Aviv")
    assert web.searched == ["Django", "Django Tel Aviv"]
    assert out.status == "resolved" and out.venue.ref.venue_id == "2"
    assert web.fetched == ["2"]


def test_the_qualified_search_uses_the_base_name():
    web = Web()
    out = resolve_one(Swept("Oscar Wilde - Irish Pub", 0, 0, *TLV),
                      web.search, web.fetch, city="Tel Aviv")
    assert web.searched == ["Oscar Wilde - Irish Pub", "Oscar Wilde Tel Aviv"]
    assert out.status == "no_match"


def test_without_a_city_there_is_no_second_search():
    web = Web()
    resolve_one(Swept("Django", 0, 0, *TLV), web.search, web.fetch)
    assert web.searched == ["Django"]


def test_a_failed_qualified_search_does_not_lose_the_first():
    r = carded("Django", "1", None)

    def search(q):
        if q.endswith("Tel Aviv"):
            raise RuntimeError("timeout")
        return [r]

    out = resolve_one(Swept("Django", 0, 0, *TLV), search,
                      lambda ref: page(r, 32.0701, 34.7846), city="Tel Aviv")
    assert out.status == "resolved"


def test_a_failed_first_search_is_search_failed():
    def search(q):
        raise RuntimeError("chrome crashed")

    out = resolve_one(Swept("Django", 0, 0, *TLV), search, None,
                      city="Tel Aviv")
    assert out.status == "search_failed" and out.name == "Django"


def test_place_rank_orders_local_unknown_elsewhere():
    assert place_rank(carded("x", "1", "Tel Aviv-Yafo, Israel"),
                      "Tel Aviv") == IN_CITY
    assert place_rank(carded("x", "1", "Tel Aviv"), "Tel Aviv-Yafo") == IN_CITY
    assert place_rank(carded("x", "1", None), "Tel Aviv") == UNKNOWN_PLACE
    assert place_rank(carded("x", "1", "Chicago, IL"), "Tel Aviv") == ELSEWHERE
    assert place_rank(carded("x", "1", "Chicago"), None) == UNKNOWN_PLACE


def test_enrich_rows_takes_csv_rows_and_keeps_them():
    row = Venue(ref=VenueRef(venue_id="", slug="", name="Nowhere",
                             category=None, address=None, city="Tel Aviv"),
                total=None, unique=None, monthly=None, you=None,
                lat=TLV[0], lng=TLV[1], geo_source="app")
    out, report = enrich_rows([row], "Tel Aviv", Web().search, Web().fetch)
    assert out[0][0] is row and out[0][1].status == "no_match"
    assert report.counts() == {"no_match": 1}


def test_enrich_rows_turns_a_swept_venue_into_a_map_row():
    out, _ = enrich_rows([Swept("Nowhere", 0, 0, *TLV)], "Tel Aviv",
                         Web().search, Web().fetch)
    v = out[0][0]
    assert v.ref.name == "Nowhere" and v.total is None and v.geo_source == "app"


# --- the real search's parser and cache ------------------------------------

CARDS = """
<div class="beer-item"><p class="name"><a href="/v/mike-s-place/1143888">
Mike's Place</a></p><p class="style">Bar</p><p class="style">123 Main St</p>
<p class="style">Chicago, IL</p></div>
<div class="beer-item"><p class="name"><a href="/v/mikes-place-tlv/9">
Mike's Place</a></p><p class="style">Pub</p>
<p class="style">Tel Aviv, תל אביב, ישראל</p></div>
<div class="beer-item"><p class="name"><a href="/user/x">not a venue</a></p></div>
<div class="beer-item"><p class="name"><a href="/v/lone/5">Lone</a></p>
<p class="style">90 Herbert Samuel</p></div>
"""


def test_parse_search_cards_reads_the_location_line():
    refs = parse_search_cards(CARDS)
    assert [(r.venue_id, r.slug) for r in refs] == [
        ("1143888", "mike-s-place"), ("9", "mikes-place-tlv"), ("5", "lone")]
    assert refs[0].city == "Chicago, IL" and refs[0].address == "123 Main St"
    assert refs[0].category == "Bar" and refs[0].name == "Mike's Place"
    assert refs[1].city.startswith("Tel Aviv") and refs[1].category == "Pub"
    # A lone numbered line is an address, never a city.
    assert refs[2].city is None and refs[2].address == "90 Herbert Samuel"
    assert parse_search_cards("<html></html>") == []


def test_search_url_is_encoded():
    assert search_url_for("Oak & Ash") == (
        "https://untappd.com/search?q=Oak+%26+Ash&type=venues")


def test_browser_search_caches_so_a_rerun_costs_nothing():
    loads: list[str] = []

    class Fake(BrowserNameSearch):
        def _load(self, query):
            loads.append(query)
            return [carded("Lauter", "1", "Tel Aviv")]

    with Fake(Settings()) as search:
        assert search("Lauter")[0].venue_id == "1"
    with Fake(Settings()) as search:
        again = search("Lauter")
    assert loads == ["Lauter"]
    assert again == [carded("Lauter", "1", "Tel Aviv")]


def test_browser_search_pacing_honours_the_hourly_budget():
    class Spent:
        def remaining(self):
            return 0

        def record(self):
            raise AssertionError("recorded past the budget")

    with pytest.raises(BudgetExceeded):
        BrowserNameSearch(Settings(), budget=Spent())._pace()


def test_page_fetcher_parses_through_the_client():
    html = ('<div class="stats"><ul><li>1,234 Total</li><li>56 Unique</li>'
            '<li>7 Monthly</li></ul></div>'
            '<div data-latitude="32.07" data-longitude="34.78"></div>')
    ref = carded("Lauter", "1", "Tel Aviv")

    class Client:
        def get(self, url):
            assert url == "https://untappd.com/v/lauter/1"
            return html

    v = page_fetcher(Client())(ref)
    assert (v.total, v.unique, v.monthly) == (1234, 56, 7)
    assert (v.lat, v.lng) == (32.07, 34.78)
