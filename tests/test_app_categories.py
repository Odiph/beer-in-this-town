"""Spending the result-set cap on drinking venues instead of highways.

A search returns at most ~60 venues, and the app's index is not a drinking
index: a Tel Aviv viewport spent its slots on `Ramat Gan National Park`,
`Crowne Plaza`, `Expo Tel Aviv` and `Yad-Eliyahu Arena`. Worse, two of the
four deep venues that cleared a 200 check-in bar turned out to be a
supermarket chain and **a highway** -- places people check in beer, not
places to drink it.

This cannot be fixed after harvesting, because **pins carry only a name and a
position**. The category lives in the filter panel, so the filter has to be
set before the search.

The panel is scrollable and its rows come and go, so the driver works from
what each dump actually shows rather than from fixed coordinates.

No device, no network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.app_categories import (
    DRINKING_CATEGORIES,
    CategoryPanelError,
    is_drinking_category,
    plan_category_taps,
)
from beer_in_this_town.app_map import categories_in


def panel(rows):
    nodes = "".join(
        f'<node class="android.widget.CheckBox" content-desc="{name}, {n}, " '
        f'checked="{str(chk).lower()}" bounds="[0,{254 + i * 82}][900,{336 + i * 82}]"/>'
        for i, (name, n, chk) in enumerate(rows))
    return ("<?xml version='1.0'?><hierarchy>"
            '<node class="android.widget.TextView" text="Filter by Category"'
            ' bounds="[132,58][370,99]"/>'
            '<node class="android.widget.Button" content-desc="DESELECT ALL"'
            ' bounds="[24,218][165,242]"/>'
            f"{nodes}</hierarchy>")


# --- what counts as a drinking place -------------------------------------

@pytest.mark.parametrize("name", [
    "Bar", "Pub", "Beer Bar", "Brewery", "Beer Garden", "Beer Store",
    "Dive Bar", "Gastropub", "Liquor Store",
    "Irish Pub", "Taproom", "Brewpub", "Hotel Bar",
])
def test_drinking_categories_are_kept(name):
    assert is_drinking_category(name) is True


@pytest.mark.parametrize("name", [
    "Supermarket", "Stadium", "Soccer Stadium", "Market", "Park",
    "Playground", "Hotel", "Convention Center", "Residential Building",
    "Pizza Place", "Coffee Shop", "Basketball Stadium", "Harbor / Marina",
    "Wine Bar", "Cocktail Bar", "Winery", "Distillery",
])
def test_everything_else_is_dropped(name):
    """`Supermarket` and a highway both cleared a 200 check-in bar in the
    measured sample. Numbers attached to a place do not make it a venue."""
    assert is_drinking_category(name) is False


def test_hotel_bar_is_kept_although_hotel_is_not():
    """The distinction is the point: a hotel is not a drinking venue, a hotel
    bar is. Substring matching on `Hotel` would drop both or keep both."""
    assert is_drinking_category("Hotel Bar") is True
    assert is_drinking_category("Hotel") is False
    assert is_drinking_category("Hotel, Event Space") is False


def test_a_compound_category_counts_if_any_part_drinks():
    """The app gives venues several categories at once, e.g.
    `Bar, Diner` or `Beer Store, Hobby Shop, Building`."""
    assert is_drinking_category("Bar, Diner") is True
    assert is_drinking_category("Beer Store, Hobby Shop, Building") is True
    assert is_drinking_category("Wings Joint, Steakhouse") is False


def test_the_keep_list_is_not_empty_and_is_lowercase():
    assert DRINKING_CATEGORIES
    assert all(c == c.casefold() for c in DRINKING_CATEGORIES)


# --- driving the panel ----------------------------------------------------

def test_only_unchecked_drinking_rows_are_tapped():
    """After `DESELECT ALL` everything is off, so each wanted row needs one
    tap. Tapping a row that is already on would turn it back off."""
    xml = panel([("Bar", 6, False), ("Hotel", 4, False), ("Pub", 4, True)])
    taps = plan_category_taps(categories_in(xml), xml)
    assert [t.name for t in taps] == ["Bar"]


def test_a_checked_junk_row_is_tapped_off():
    xml = panel([("Bar", 6, True), ("Supermarket", 2, True)])
    taps = plan_category_taps(categories_in(xml), xml)
    assert [t.name for t in taps] == ["Supermarket"]


def test_taps_land_in_the_middle_of_their_row():
    xml = panel([("Bar", 6, False)])
    tap = plan_category_taps(categories_in(xml), xml)[0]
    assert 254 < tap.y < 336
    assert 0 < tap.x < 900


def test_a_row_with_no_count_is_still_actionable():
    """The bottom row is clipped by the screen edge and never renders its
    number. Skipping it would leave a junk category selected."""
    xml = ("<?xml version='1.0'?><hierarchy>"
           '<node class="android.widget.TextView" text="Filter by Category" bounds="[1,1][2,2]"/>'
           '<node class="android.widget.CheckBox" content-desc="Supermarket"'
           ' checked="true" bounds="[0,1574][900,1600]"/></hierarchy>')
    assert [t.name for t in plan_category_taps(categories_in(xml), xml)] == ["Supermarket"]


def test_nothing_to_do_is_an_empty_plan_not_an_error():
    xml = panel([("Bar", 6, True), ("Pub", 4, True)])
    assert plan_category_taps(categories_in(xml), xml) == []


def test_a_dump_that_is_not_the_category_panel_is_refused():
    """Tapping blind through a screen that is not the panel would toggle
    whatever happens to sit at those coordinates."""
    with pytest.raises(CategoryPanelError):
        plan_category_taps({}, "<?xml version='1.0'?><hierarchy/>")
