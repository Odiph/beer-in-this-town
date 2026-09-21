"""The region appended to each Maps lookup must come from the data.

`--region` defaulted to "Singapore" for every city. A London CSV therefore
searched "BrewDog, 12 High St, London, Singapore" -- and with the old fuzzy
list matching, what came back could be saved and journalled ok. The guard
exists so a name cannot match the wrong country; hardcoding one country is the
inverse of that.
"""
from __future__ import annotations

import csv

import pytest

from beer_in_this_town.pin_to_list import region_from_csv


def _csv(tmp_path, rows, fields=("name", "address", "city")):
    path = tmp_path / "venues.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(fields))
        w.writeheader()
        w.writerows(rows)
    return path


@pytest.mark.unit
def test_the_region_comes_from_the_citys_own_rows(tmp_path):
    path = _csv(tmp_path, [
        {"name": "Ghost Whale", "address": "24 Clapham High St", "city": "London"},
        {"name": "The Kernel", "address": "11 Dockley Rd", "city": "London"},
    ])
    assert region_from_csv(path) == "London"


@pytest.mark.unit
def test_the_most_common_city_wins_over_a_stray_row(tmp_path):
    """One venue filed under a suburb must not redirect the whole run."""
    path = _csv(tmp_path, [
        {"name": "A", "address": "1 Rd", "city": "London"},
        {"name": "B", "address": "2 Rd", "city": "London"},
        {"name": "C", "address": "3 Rd", "city": "Croydon"},
    ])
    assert region_from_csv(path) == "London"


@pytest.mark.unit
def test_no_city_column_means_no_region_rather_than_a_guess(tmp_path):
    """The seed CSV has no city column. Appending anything would be invention."""
    path = _csv(tmp_path, [{"name": "A", "address": "1 Rd"}],
                fields=("name", "address"))
    assert region_from_csv(path) is None


@pytest.mark.unit
def test_blank_cities_are_not_a_region(tmp_path):
    path = _csv(tmp_path, [{"name": "A", "address": "1 Rd", "city": ""}])
    assert region_from_csv(path) is None


@pytest.mark.unit
def test_an_explicit_empty_region_is_not_re_derived(tmp_path):
    """`--region ''` switches the guard off deliberately.

    Collapsing it to None would send it back through derivation and quietly
    re-enable the thing the caller just turned off.
    """
    from beer_in_this_town.cli import resolve_region

    path = _csv(tmp_path, [{"name": "A", "address": "1 Rd", "city": "London"}])
    assert resolve_region("", path) == (None, [])
    assert resolve_region(None, path) == ("London", [])


@pytest.mark.unit
def test_a_csv_with_no_city_says_the_guard_is_off(tmp_path):
    """Silently appending nothing looks identical to a working guard."""
    from beer_in_this_town.cli import resolve_region

    path = _csv(tmp_path, [{"name": "A", "address": "1 Rd"}],
                fields=("name", "address"))
    region, warnings = resolve_region(None, path)
    assert region is None
    assert warnings and "another country" in warnings[0]
