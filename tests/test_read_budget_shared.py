"""The hourly read ceiling is the file, not whichever object counted last.

Found by the release verification: enrich's search and its page fetches each
held a `ReadBudget` over the same file and overwrote each other's count; 20
requests alternating between them persisted as 10.

No network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.config import Settings
from beer_in_this_town.http_client import ReadBudget

pytestmark = pytest.mark.unit


def test_two_budgets_over_one_file_count_every_request(tmp_path):
    path = tmp_path / "read_budget.json"
    a, b = ReadBudget(Settings(), path), ReadBudget(Settings(), path)
    for i in range(20):
        (a if i % 2 else b).record()
    assert ReadBudget(Settings(), path).count == 20
    assert a.remaining() == Settings().hourly_budget - 20


def test_the_gated_page_is_dropped_from_the_cache():
    """Cached, a gated page would outlive the sign-in by 12h."""
    from beer_in_this_town.models import VenueRef
    from beer_in_this_town.parsers import StatsLoginRequired
    from beer_in_this_town.resolve import page_fetcher

    gated = ('<div class="stats"><a href="/login?go_to=x">Log In</a> '
             'to view Venue Stats</div>')

    class Client:
        forgotten: list[str] = []

        def get(self, url):
            return gated

        def forget(self, url):
            self.forgotten.append(url)

    client = Client()
    ref = VenueRef(venue_id="1", slug="x", name="X", category=None,
                   address=None, city=None)
    with pytest.raises(StatsLoginRequired):
        page_fetcher(client)(ref)
    assert client.forgotten == [ref.url]
