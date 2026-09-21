"""selfcheck: one known-good venue page, parsed, and nothing else.

Venue pages are the one Untappd web surface this tool still reads (enrich
joins swept names to them), so they are what the cheap early warning has to
watch. The web search it also used to probe is no longer a collection path.
No network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town import cli
from beer_in_this_town.config import Settings

# Same shape the parser tests use, so this file asserts selfcheck's behaviour
# rather than re-deriving what a venue page looks like.
VENUE_OK = """
<div class="stats">
  <li><span>20,259</span><span>Total</span></li>
  <li><span>2,451</span><span>Unique</span></li>
  <li><span>136</span><span>Monthly</span></li>
  <li><a>0</a><span>You</span></li>
</div>
"""

class _FakeClient:
    """Serves a venue page without touching the network."""

    def __init__(self, venue_html: str):
        self._venue = venue_html
        self.urls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        self.urls.append(url)
        return self._venue


@pytest.fixture
def client(monkeypatch):
    def install(venue_html=VENUE_OK):
        fake = _FakeClient(venue_html)
        monkeypatch.setattr(cli, "PoliteClient", lambda s: fake)
        return fake
    return install


@pytest.mark.unit
def test_selfcheck_reads_one_venue_page_and_no_search(client):
    fake = client()
    env = cli.cmd_selfcheck(Settings(), "ghost-whale", "1")
    assert env.ok, env.error
    assert len(fake.urls) == 1
    assert "/v/ghost-whale/1" in fake.urls[0]
    assert "search" not in env.data


@pytest.mark.unit
def test_a_broken_venue_page_is_reported_stale(client):
    client(venue_html="<div>nothing</div>")
    env = cli.cmd_selfcheck(Settings(), "ghost-whale", "1")
    assert env.ok is False
    assert env.error.code in {"selectors_stale", "stats_missing"}


@pytest.mark.unit
def test_selfcheck_points_back_at_status_not_run(client):
    client()
    env = cli.cmd_selfcheck(Settings(), "ghost-whale", "1")
    assert env.next_actions == ["python -m beer_in_this_town status --json"]
