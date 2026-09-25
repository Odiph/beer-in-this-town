"""Identifying a saved list by name has to be exact, in all three places.

`pin` matched the list by substring when picking the row, when verifying what
Maps said afterwards, and when checking the list exists at all. An account with
"Bars" and "London Bars" could therefore have a place saved into the wrong one
AND have that verified as correct -- the exact wrong-list failure the module
docstring says it exists to prevent. Offline: these are pure string decisions.
"""
from __future__ import annotations

import pytest

from beer_in_this_town import pin_to_list
from beer_in_this_town.pin_to_list import (
    AmbiguousList,
    list_exists_in,
    pick_list_row,
    saved_in_names,
    saved_in_target,
)


# --- reading what Maps says a place is saved in ---------------------------
@pytest.mark.unit
@pytest.mark.parametrize("text,expected", [
    ("London Bars", {"London Bars"}),
    ("Bars, London Bars", {"Bars", "London Bars"}),
    ("  Favourites , Want to go ", {"Favourites", "Want to go"}),
    ("", set()),
])
def test_saved_in_names_splits_multiple_lists(text, expected):
    assert saved_in_names(text) == expected


@pytest.mark.unit
def test_a_place_in_a_similarly_named_list_is_not_a_match():
    """The bug: "Bars" is a substring of "London Bars", so it verified as ok."""
    assert saved_in_target("London Bars", "Bars") is False
    assert saved_in_target("Bars", "Bars") is True


@pytest.mark.unit
def test_one_of_several_lists_still_counts():
    """A place may be in more than one list; ours only has to be among them."""
    assert saved_in_target("Bars, London Bars", "Bars") is True
    assert saved_in_target("Favourites, London Bars", "London Bars") is True


@pytest.mark.unit
def test_case_and_padding_do_not_decide_identity():
    assert saved_in_target("  london bars ", "London Bars") is True


# --- picking the row in the list picker -----------------------------------
@pytest.mark.unit
def test_the_exactly_named_row_wins_over_a_longer_one():
    """Maps offers both; taking .first is how the wrong list gets clicked."""
    assert pick_list_row(["London Bars", "Bars"], "Bars") == 1


@pytest.mark.unit
def test_a_row_label_carrying_a_place_count_still_matches():
    """Accessible names often read "Bars (12)" -- that is the same list."""
    assert pick_list_row(["London Bars (40)", "Bars (12)"], "Bars") == 1


@pytest.mark.unit
def test_no_exact_row_is_refused_rather_than_approximated():
    """Clicking the nearest label is how a place lands in a stranger's list."""
    with pytest.raises(AmbiguousList, match="Bars"):
        pick_list_row(["London Bars", "Singapore Bars"], "Bars")


@pytest.mark.unit
def test_two_rows_with_the_same_name_are_refused():
    """Duplicate list names exist; guessing between them is not ours to do."""
    with pytest.raises(AmbiguousList):
        pick_list_row(["Bars", "Bars"], "Bars")


# --- the pre-flight existence check ---------------------------------------
@pytest.mark.unit
def test_list_exists_requires_a_whole_line_not_a_substring():
    body = "Your lists\nLondon Bars\n40 places\nFavourites\n"
    assert list_exists_in(body, "London Bars") is True
    assert list_exists_in(body, "Bars") is False, \
        "substring matching here is what let the whole run start wrong"


@pytest.mark.unit
def test_list_exists_ignores_a_trailing_place_count():
    assert list_exists_in("Your lists\nBars (12)\n", "Bars") is True


class SlowListsPage:
    """The saved-lists panel renders after a while, as Maps does."""

    def __init__(self, renders_after: int, lists: str):
        self.reads, self.renders_after, self.lists = 0, renders_after, lists
        self.waited_ms = 0

    def locator(self, selector):
        return self

    def inner_text(self, timeout=None):
        self.reads += 1
        shell = "Search Google Maps\nSaved\nRecents\n"
        return shell + (self.lists if self.reads > self.renders_after else "")

    def wait_for_timeout(self, ms):
        self.waited_ms += ms


@pytest.mark.unit
def test_the_list_check_waits_for_a_slow_panel():
    # Found live 2026-09-25: read once after a fixed 4 s, the panel had not
    # drawn yet and an existing list was reported missing (list_missing).
    page = SlowListsPage(renders_after=3,
                         lists="London Bars Test\nPrivate · 33 places\n")
    assert pin_to_list.saved_lists_show(page, "London Bars Test") is True
    assert page.reads == 4


@pytest.mark.unit
def test_a_list_that_never_appears_is_missing_after_the_deadline():
    page = SlowListsPage(renders_after=0, lists="Other list\nPrivate · 1 place\n")
    assert pin_to_list.saved_lists_show(page, "London Bars Test",
                                        timeout_s=10) is False
    assert page.waited_ms >= 10_000
