"""The city's search variants: which spellings of its name to search.

Counts are the shape measured in Tel Aviv and London from Foursquare's open
places (2026-09-23): a handful of spellings carry the city, a long tail of
typos and districts carries the rest.
"""
import json

import pytest

from beer_in_this_town import city_names
from beer_in_this_town.city_names import CityNames, norm, pick_variants


def test_norm_folds_case_dashes_and_a_trailing_country():
    assert norm("Tel Aviv-Yafo") == "tel aviv yafo"
    assert norm("Tel Aviv-Jaffa, Israel") == "tel aviv jaffa"
    assert norm("  תל אביב–יפו ") == "תל אביב יפו"
    assert norm("Giv'atayim") == "giv atayim"


def test_a_longer_spelling_is_folded_into_the_shorter_one_it_contains():
    # Untappd matches whole words, so "tel aviv" catches "tel aviv yafo" lines.
    picked = pick_variants({"Tel Aviv": 60, "Tel Aviv-Yafo": 30, "Jaffa": 10})
    assert [v.query for v in picked] == ["tel aviv", "jaffa"]
    assert picked[0].places == 90


def test_spellings_under_two_percent_are_dropped():
    counts = {"London": 950, "Croydon": 30, "Londres": 15, "Lodnon": 5}
    assert [v.query for v in pick_variants(counts)] == ["london", "croydon"]


def test_a_misspelling_does_not_absorb_the_real_name():
    # "tel" alone is a typo-level fragment: rare (0.1%), so it is no cover,
    # and the real name is not folded into it.
    counts = {"Tel Aviv": 1000, "Tel": 1}
    assert [v.query for v in pick_variants(counts)] == ["tel aviv"]


def test_at_most_ten_variants_most_places_first():
    counts = {f"Town {chr(97 + i)}": 100 - i for i in range(15)}
    picked = pick_variants(counts)
    assert len(picked) == 10
    assert picked[0].query == "town a"
    assert [v.places for v in picked] == sorted((v.places for v in picked),
                                               reverse=True)


def test_every_script_competes_on_share_alone():
    counts = {"Tel Aviv": 687, "תל אביב": 235, "Jaffa": 21, "Тель-Авив": 9}
    queries = [v.query for v in pick_variants(counts)]
    assert queries == ["tel aviv", "תל אביב", "jaffa"]


def test_no_named_places_means_no_variants():
    assert pick_variants({}) == []


def _cached(tmp_path, monkeypatch, city="Tel Aviv"):
    monkeypatch.setattr(city_names, "GAZETTEER_DIR", tmp_path)
    (tmp_path / "tel-aviv.json").write_text(json.dumps({
        "city": city, "variants": ["tel aviv", "תל אביב", "jaffa"],
        "bbox": [31.9, 32.2, 34.7, 34.9], "source": "fsq"}), encoding="utf-8")


def test_a_cached_city_is_read_without_building(tmp_path, monkeypatch):
    _cached(tmp_path, monkeypatch)
    monkeypatch.setattr(city_names, "_build",
                        lambda *a, **k: pytest.fail("rebuilt a cached city"))
    names = city_names.for_city("Tel Aviv", settings=None)
    assert names.variants == ("tel aviv", "תל אביב", "jaffa")
    assert names.contains(32.07, 34.78)
    assert not names.contains(41.35, -72.1)  # New London, CT


def test_without_foursquare_the_city_as_typed_is_the_only_variant(
        tmp_path, monkeypatch):
    monkeypatch.setattr(city_names, "GAZETTEER_DIR", tmp_path)

    def unavailable(*_a, **_k):
        raise city_names.NamesUnavailable("duckdb is not installed")

    monkeypatch.setattr(city_names, "_build", unavailable)
    names = city_names.for_city("Tel Aviv", settings=None)
    assert names.variants == ("tel aviv",)
    assert names.bbox is None and names.contains(0.0, 0.0)
    assert "duckdb is not installed" in names.warning
    assert not (tmp_path / "tel-aviv.json").exists()  # a fallback is not cached


def test_the_bounding_box_check_is_inclusive():
    names = CityNames(city="x", variants=("x",), bbox=(1.0, 2.0, 3.0, 4.0))
    assert names.contains(1.0, 3.0) and names.contains(2.0, 4.0)
    assert not names.contains(2.01, 3.5)
