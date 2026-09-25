"""`sweep --method search`: the city's venues from Untappd's own web search.

Measured 2026-09-23 against the app-map sweeps: the popularity sort's top
1,000 for "london" held 99 of the London sweep's top 100 venues by
check-ins (50 requests); "תל אביב" held every one of Tel Aviv's top 100
beer venues, of which the map sweep had 20.
"""
import csv

import pytest

from beer_in_this_town import search_sweep
from beer_in_this_town.city_names import CityNames
from beer_in_this_town.models import VenueRef
from beer_in_this_town.search_sweep import SearchPage, harvest


def ref(vid, name, city="London, Greater London, United Kingdom"):
    return VenueRef(venue_id=str(vid), slug=name.lower().replace(" ", "-"),
                    name=name, category="Pub", address="1 High St", city=city)


LONDON = CityNames(city="London", variants=("london", "croydon"),
                   bbox=(51.28, 51.70, -0.51, 0.34))


def pages(table):
    calls = []

    def load(query, top):
        calls.append((query, top))
        total, refs = table[query]
        return SearchPage(total=total, refs=refs[:top])

    return load, calls


def test_each_variant_is_searched_once_to_the_depth_asked():
    load, calls = pages({"london": (63300, [ref(1, "A"), ref(2, "B")]),
                         "croydon": (995, [ref(3, "C", "Croydon, Greater "
                                                      "London, UK")])})
    h = harvest(LONDON, load, top=1000)
    assert calls == [("london", 1000), ("croydon", 1000)]
    assert [v.ref.venue_id for v in h.venues] == ["1", "2", "3"]


def test_a_venue_found_by_two_variants_is_kept_once_where_first_found():
    load, _ = pages({"london": (2, [ref(1, "A"), ref(3, "C")]),
                     "croydon": (1, [ref(3, "C")])})
    h = harvest(LONDON, load, top=1000)
    assert [v.ref.venue_id for v in h.venues] == ["1", "3"]


def test_a_name_match_from_another_city_line_is_dropped():
    # "Tel Aviv Pizza" in the Netherlands matches on its name, not its city.
    names = CityNames(city="Tel Aviv", variants=("תל אביב", "tel aviv"))
    load, _ = pages({
        "תל אביב": (1, [ref(1, "Lauter", "Tel Aviv, תל אביב, ישראל")]),
        "tel aviv": (2, [ref(2, "Tel Aviv Pizza", "Nederland"),
                         ref(3, "Satchmo", "Tel Aviv District, ישראל")])})
    h = harvest(names, load, top=1000)
    assert [v.ref.name for v in h.venues] == ["Lauter", "Satchmo"]
    assert h.per_variant["tel aviv"]["other_city"] == 1


def test_search_results_carry_ids_and_no_invented_numbers():
    load, _ = pages({"london": (1, [ref(7, "The Rake")]), "croydon": (0, [])})
    v = harvest(LONDON, load, top=1000).venues[0]
    assert v.ref.url == "https://untappd.com/v/the-rake/7"
    assert (v.total, v.lat, v.lng) == (None, None, None)


def test_a_capped_variant_says_less_popular_venues_were_not_collected():
    load, _ = pages({"london": (63300, [ref(i, f"V{i}") for i in range(5)]),
                     "croydon": (3, [])})
    h = harvest(LONDON, load, top=5)
    assert h.per_variant["london"] == {"total": 63300, "collected": 5,
                                       "other_city": 0, "complete": True}
    assert any("london" in w and "most-checked-in" in w for w in h.warnings)


def test_top_is_clamped_to_the_search_cap():
    load, calls = pages({"london": (1, []), "croydon": (1, [])})
    harvest(LONDON, load, top=5000)
    assert {top for _, top in calls} == {search_sweep.SEARCH_CAP}


def test_the_command_writes_the_sweep_stage_and_offers_enrich(
        tmp_path, monkeypatch):
    monkeypatch.setattr(search_sweep, "stage_path",
                        lambda city, name: tmp_path / name)
    monkeypatch.setattr(search_sweep, "record_run", lambda **_k: None)
    load, _ = pages({"london": (2, [ref(1, "A"), ref(2, "B")]),
                     "croydon": (0, [])})
    env = search_sweep.cmd_search_sweep(
        None, "London", top=1000, formats=(), load=load, names=LONDON)
    assert env.ok and env.data["method"] == "search"
    assert env.data["venues"] == 2 and env.data["located"] == 0
    assert env.next_actions == [
        'python -m beer_in_this_town enrich --city "London" --json']
    with open(tmp_path / "1_sweep.csv", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["venue_id"] for r in rows] == ["1", "2"]
    assert rows[0]["url"] == "https://untappd.com/v/a/1"


def test_nothing_found_is_a_failure_not_an_empty_city(tmp_path, monkeypatch):
    monkeypatch.setattr(search_sweep, "stage_path",
                        lambda city, name: tmp_path / name)
    load, _ = pages({"london": (0, []), "croydon": (0, [])})
    env = search_sweep.cmd_search_sweep(
        None, "London", top=1000, formats=(), load=load, names=LONDON)
    assert not env.ok and env.error.code == "city_not_found"
    assert not (tmp_path / "1_sweep.csv").exists()


@pytest.mark.parametrize("top", [0, -3])
def test_top_must_be_positive(top):
    with pytest.raises(ValueError):
        harvest(LONDON, lambda q, t: SearchPage(0, []), top=top)


def test_an_unreadable_search_page_is_its_own_error(tmp_path, monkeypatch):
    """Step 1 of verify-change: a bare RuntimeError here reached the user as
    `unexpected_error`, whose remedy ("re-run with -v") is the wrong one."""
    monkeypatch.setattr(search_sweep, "stage_path",
                        lambda city, name: tmp_path / name)

    def broken(query, top):
        raise search_sweep.SearchPageUnreadable("neither results nor empty")

    env = search_sweep.cmd_search_sweep(
        None, "London", top=1000, formats=(), load=broken, names=LONDON)
    assert not env.ok and env.error.code == "search_unavailable"
    assert "verify --json" in env.error.remedy
    assert not (tmp_path / "1_sweep.csv").exists()


def test_a_search_sweep_records_its_method(tmp_path, monkeypatch):
    """Review finding: `_run` called record_run without a method, so "" fell
    through to the intent, and a dashboard `map` choice silently won over the
    search sweep that had just run. Not stubbed: the real record_run."""
    from beer_in_this_town import state

    state.record_intent("London", method="map")
    monkeypatch.setattr(search_sweep, "stage_path",
                        lambda city, name: tmp_path / name)
    load, _ = pages({"london": (1, [ref(1, "A")]), "croydon": (0, [])})
    assert search_sweep.cmd_search_sweep(
        None, "London", top=1000, formats=(), load=load, names=LONDON).ok
    assert state.sweep_method(None) == "search"


def test_a_variant_that_stopped_loading_is_not_passed_off_as_the_tail():
    # Measured in the Tel Aviv probe: a "Show More" that never rendered left
    # 440 of 465 results, and a timed-out first page read as 0 venues. Short
    # because a page failed is not short because the rest are unpopular.
    load, _ = pages({"london": (465, [ref(1, "A"), ref(2, "B")])})

    def stalled(query, top):
        page = load(query, top)
        return SearchPage(total=page.total, refs=page.refs, complete=False)

    h = harvest(CityNames(city="London", variants=("london",)), stalled,
                top=1000)
    assert [v.ref.venue_id for v in h.venues] == ["1", "2"]
    assert h.per_variant["london"]["complete"] is False
    text = " ".join(h.warnings).lower()
    assert "stopped loading" in text and "re-run" in text
    assert "less-visited" not in text


def test_an_incomplete_search_is_not_cached(tmp_path, monkeypatch):
    # A cached short page would be served again for cache_ttl_s: the re-run
    # the warning asks for would retry nothing.
    from beer_in_this_town import config

    monkeypatch.setattr(search_sweep, "CACHE_DIR", tmp_path)
    s = config.Settings()
    search = search_sweep.BrowserCitySearch.__new__(search_sweep.BrowserCitySearch)
    search.s = s
    results = iter([SearchPage(465, [ref(1, "A")], complete=False),
                    SearchPage(465, [ref(1, "A"), ref(2, "B")])])
    monkeypatch.setattr(search, "_load", lambda q, t: next(results),
                        raising=False)
    assert search("london", 1000).complete is False
    assert len(search("london", 1000).refs) == 2   # fetched again, not cached
    assert len(search("london", 1000).refs) == 2   # now complete: cached
