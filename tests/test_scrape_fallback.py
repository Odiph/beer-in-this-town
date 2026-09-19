"""The search path falls back to the browser when the page is client-rendered.

Untappd moved search to Algolia, so the HTTP response now carries an empty
`#algolia-hits` container and no `.beer-item` nodes. `parse_search_page` raises
`ParseError` for that, which used to abort the whole run even though driving a
real browser would have worked. No network, no browser: both search paths are
stubbed, and the test only asserts which one the dispatcher chose.
"""
from __future__ import annotations

import pytest

from beer_in_this_town import scrape
from beer_in_this_town.models import VenueRef
from beer_in_this_town.parsers import ParseError

REFS = [VenueRef(venue_id="1", slug="ghost-whale", name="Ghost Whale",
                 category="Beer Bar", address=None, city="London")]


def _stub(monkeypatch, http_raises):
    def fake_http(client, s):
        raise http_raises

    monkeypatch.setattr(scrape, "search_via_http", fake_http)
    monkeypatch.setattr(scrape, "search_via_browser", lambda s: REFS)


@pytest.mark.unit
def test_parse_error_falls_back_to_browser(monkeypatch):
    """A client-rendered search page must not abort the run."""
    _stub(monkeypatch, ParseError("No .beer-item nodes on the search page."))
    assert scrape.collect_venue_refs(client=None, s=None) == REFS


@pytest.mark.unit
def test_pagination_unsupported_still_falls_back(monkeypatch):
    """The pre-existing fallback is unchanged."""
    _stub(monkeypatch, scrape.PaginationUnsupported("no offset param worked"))
    assert scrape.collect_venue_refs(client=None, s=None) == REFS


@pytest.mark.unit
def test_unrelated_errors_still_propagate(monkeypatch):
    """Only the two known search failures fall back; real bugs surface."""
    _stub(monkeypatch, ValueError("something genuinely wrong"))
    with pytest.raises(ValueError):
        scrape.collect_venue_refs(client=None, s=None)
