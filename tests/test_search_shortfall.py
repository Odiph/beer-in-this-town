"""A short or unreadable search result set must say so.

Untappd's move to Algolia made two quiet failures reachable on the happy path:
an empty client-rendered shell that used to be dumped to debug/ on every single
run, and an anonymous five-result cap that produced a five-row CSV and a green
envelope. Both are covered here. No network, no browser.
"""
from __future__ import annotations

import pytest

from beer_in_this_town import scrape
from beer_in_this_town.models import VenueRef
from beer_in_this_town.parsers import (
    ClientRenderedSearch,
    ParseError,
    parse_search_page,
)
from beer_in_this_town.scrape import SearchLoginRequired

# The shell Untappd serves over plain HTTP: the container is there, the rows
# are not, because Algolia injects them once JS runs.
ALGOLIA_SHELL = """
<div id="algolia-stats">58,685 venue results for "london"</div>
<div id="algolia-hits"></div>
<div class="item" data-track="sidebar"><a href="/b/some-beer/123">Not a venue</a></div>
"""

# No .beer-item and no Algolia container either: this is what a genuine markup
# change looks like, and it must stay loud.
CHANGED_MARKUP = '<div id="something-else-entirely"><p>Nothing we recognise</p></div>'

LOGIN_GATE = """
<div id="algolia-hits"><div class="beer-item"></div></div>
<div id="algolia-login-gate">Please sign in to view more.</div>
"""

REFS = [VenueRef(venue_id="1", slug="ghost-whale", name="Ghost Whale",
                 category="Beer Bar", address=None, city="London")]


@pytest.mark.unit
def test_algolia_shell_is_its_own_error(monkeypatch):
    """The expected client-rendered shell must not be filed as a broken selector."""
    dumped = []
    monkeypatch.setattr(
        "beer_in_this_town.parsers.dump_debug",
        lambda name, html: dumped.append(name),
    )
    with pytest.raises(ClientRenderedSearch):
        parse_search_page(ALGOLIA_SHELL)
    assert dumped == [], "the shell is the normal HTTP response, not debug material"


@pytest.mark.unit
def test_changed_markup_still_dumps_and_raises(monkeypatch):
    """A real selector break keeps the loud path: plain ParseError plus a dump."""
    dumped = []
    monkeypatch.setattr(
        "beer_in_this_town.parsers.dump_debug",
        lambda name, html: dumped.append(name),
    )
    with pytest.raises(ParseError) as exc:
        parse_search_page(CHANGED_MARKUP)
    assert not isinstance(exc.value, ClientRenderedSearch)
    assert dumped == ["search_page_no_items"]


@pytest.mark.unit
def test_shell_still_falls_back_to_the_browser(monkeypatch):
    """Narrowing the exception must not narrow the fallback."""
    def fake_http(client, s):
        raise ClientRenderedSearch("empty #algolia-hits")

    monkeypatch.setattr(scrape, "search_via_http", fake_http)
    monkeypatch.setattr(scrape, "search_via_browser", lambda s: REFS)
    assert scrape.collect_venue_refs(client=None, s=None) == REFS


@pytest.mark.unit
def test_login_gate_raises_rather_than_returning_five():
    """Five venues behind a sign-in wall is a stopped run, not a small city."""
    with pytest.raises(SearchLoginRequired):
        scrape.assert_not_login_gated(REFS, LOGIN_GATE, target_count=100)


@pytest.mark.unit
def test_no_gate_means_a_short_result_set_is_legitimate():
    """A query that genuinely has few venues must still succeed."""
    scrape.assert_not_login_gated(REFS, ALGOLIA_SHELL, target_count=100)


@pytest.mark.unit
def test_a_full_result_set_is_never_gated():
    """Reaching the target means paging worked, whatever else is on the page."""
    scrape.assert_not_login_gated(REFS, LOGIN_GATE, target_count=1)
