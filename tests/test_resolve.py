"""Joining a map-swept name to its Untappd venue page.

The app sweep knows a venue's name and roughly where it is; the venue page
knows its id, its counts and its real coordinates. The join is by name, and
names are the weak part: they are bilingual in the app, generic everywhere
(`Beer Garden`), and shared by chains a kilometre apart. So a name match is
only a *candidate*. It is accepted when the page's own coordinates land near
where the map put the pin, and not otherwise.

The failure this guards against is quiet: a Berlin bar's check-ins attached
to a Tel Aviv pin, in a corpus whose every row looks filled in.

Search and fetch are injected. No browser, no network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.app_sweep import Venue as Swept
from beer_in_this_town.http_client import RateLimitTripped
from beer_in_this_town.models import Venue, VenueRef
from beer_in_this_town.resolve import (
    MAX_FETCHES_PER_NAME,
    enrich,
    name_score,
    resolve_one,
)

TLV = (32.0700, 34.7846)


def ref(name, vid="1"):
    return VenueRef(venue_id=vid, slug=name.lower().replace(" ", "-"),
                    name=name, category="Bar", address=None, city="Tel Aviv")


def page(r, lat, lng, total=1000):
    return Venue(ref=r, total=total, unique=100, monthly=10, you=None,
                 lat=lat, lng=lng, geo_source="embedded")


class Web:
    """Search results by query, venue pages by id, and a record of calls."""

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


# --- names ------------------------------------------------------------------

def test_a_bilingual_app_name_matches_its_latin_web_name():
    assert name_score("Mike's Place (מייקס פלייס)", "Mike's Place") >= 0.9


def test_a_hebrew_only_name_matches_itself():
    """Stripping to ASCII once collapsed every Hebrew name to "" and matched
    them all to each other."""
    assert name_score("שר המשקאות", "שר המשקאות") == 1.0
    assert name_score("שר המשקאות", "בירה בשוק") < 0.5


def test_unrelated_names_do_not_match():
    assert name_score("Lauter", "Oscar Wilde") < 0.5


def test_chain_branches_are_told_apart_by_name():
    assert (name_score("Beer Bazaar Habima", "Beer Bazaar Habima")
            > name_score("Beer Bazaar Habima", "Beer Bazaar Carmel"))


# --- one venue --------------------------------------------------------------

def test_a_nearby_name_match_is_accepted_with_the_pages_own_data():
    r = ref("Lauter", "12033151")
    web = Web({"Lauter": [r]}, {"12033151": page(r, 32.07037, 34.784649, 53056)})
    out = resolve_one(Swept("Lauter", 0, 0, *TLV), web.search, web.fetch)
    assert out.status == "resolved"
    assert out.venue.total == 53056
    assert out.venue.ref.venue_id == "12033151"
    # The page's coordinates win over the map fit.
    assert (out.venue.lat, out.venue.lng) == (32.07037, 34.784649)


def test_a_name_match_in_another_city_is_refused():
    """The whole point. `Beer Garden` exists in every city on earth."""
    r = ref("Beer Garden", "9")
    web = Web({"Beer Garden": [r]}, {"9": page(r, 52.52, 13.40)})  # Berlin
    out = resolve_one(Swept("Beer Garden", 0, 0, *TLV), web.search, web.fetch)
    assert out.status == "too_far"
    assert out.venue is None


def test_the_nearer_branch_of_a_chain_wins_when_the_first_is_too_far():
    far, near = ref("Beer Garden", "1"), ref("Beer Garden", "2")
    web = Web({"Beer Garden": [far, near]},
              {"1": page(far, 52.52, 13.40), "2": page(near, 32.0705, 34.7850)})
    out = resolve_one(Swept("Beer Garden", 0, 0, *TLV), web.search, web.fetch)
    assert out.status == "resolved"
    assert out.venue.ref.venue_id == "2"


def test_fetches_per_name_are_capped():
    """Each fetch is a real request against a paced budget."""
    cands = [ref("Beer Garden", str(i)) for i in range(5)]
    web = Web({"Beer Garden": cands},
              {str(i): page(cands[i], 52.52, 13.40) for i in range(5)})
    resolve_one(Swept("Beer Garden", 0, 0, *TLV), web.search, web.fetch)
    assert len(web.fetched) <= MAX_FETCHES_PER_NAME


def test_no_name_close_enough_fetches_nothing():
    web = Web({"Lauter": [ref("Oscar Wilde")]})
    out = resolve_one(Swept("Lauter", 0, 0, *TLV), web.search, web.fetch)
    assert out.status == "no_match"
    assert web.fetched == []


def test_a_page_without_coordinates_cannot_confirm_identity():
    """Unverifiable is not verified. Its counts might be another bar's."""
    r = ref("Lauter", "5")
    web = Web({"Lauter": [r]}, {"5": page(r, None, None)})
    out = resolve_one(Swept("Lauter", 0, 0, *TLV), web.search, web.fetch)
    assert out.status == "unverified"
    assert out.venue is None


def test_a_swept_venue_with_no_position_is_not_looked_up():
    """With nothing to check a match against, any match is a guess."""
    web = Web({"Lauter": [ref("Lauter")]})
    out = resolve_one(Swept("Lauter", 0, 0), web.search, web.fetch)
    assert out.status == "unlocated"
    assert web.searched == []


def test_a_failed_page_is_that_venues_problem_not_the_runs():
    r = ref("Lauter", "5")

    def boom(_):
        raise RuntimeError("500")

    web = Web({"Lauter": [r]})
    out = resolve_one(Swept("Lauter", 0, 0, *TLV), web.search, boom)
    assert out.status == "fetch_failed"


# --- the whole sweep --------------------------------------------------------

def test_unresolved_venues_are_kept_not_dropped():
    r = ref("Lauter", "12033151")
    web = Web({"Lauter": [r]}, {"12033151": page(r, 32.07037, 34.784649)})
    swept = [Swept("Lauter", 0, 0, *TLV), Swept("Nowhere Bar", 0, 0, *TLV)]
    venues, report = enrich(swept, "Tel Aviv", web.search, web.fetch)
    assert [v.ref.name for v in venues] == ["Lauter", "Nowhere Bar"]
    kept = venues[1]
    assert kept.geo_source == "app" and kept.total is None
    assert report.counts() == {"resolved": 1, "no_match": 1}


def test_two_swept_names_resolving_to_one_page_keep_one_row():
    """The app lists `Oscar Wilde` and `Oscar Wilde - Irish Pub` separately;
    if both land on one venue page they are one venue."""
    r = ref("Oscar Wilde", "7")
    web = Web({"Oscar Wilde": [r], "Oscar Wilde - Irish Pub": [r]},
              {"7": page(r, 32.0700, 34.7846)})
    swept = [Swept("Oscar Wilde", 0, 0, *TLV),
             Swept("Oscar Wilde - Irish Pub", 0, 0, *TLV)]
    venues, report = enrich(swept, "Tel Aviv", web.search, web.fetch)
    assert [v.ref.venue_id for v in venues] == ["7"]
    assert report.counts()["duplicate"] == 1


def test_rate_limits_stop_the_run_rather_than_being_swallowed():
    """Same rule as `fetch_venues`: a deliberate stop is not a per-venue
    failure, and swallowing one fires a request per remaining venue into an
    active rate limit."""
    r = ref("Lauter", "5")

    def tripped(_):
        raise RateLimitTripped("429")

    web = Web({"Lauter": [r]})
    with pytest.raises(RateLimitTripped):
        enrich([Swept("Lauter", 0, 0, *TLV)], "Tel Aviv", web.search, tripped)
