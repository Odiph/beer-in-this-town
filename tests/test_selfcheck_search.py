"""selfcheck must be able to see the failure it exists to warn about.

Untappd moved search to Algolia and every `run` broke. selfcheck returned
ok: true throughout, because it fetches one venue detail page -- which is
server-rendered and was never affected -- and nothing else. AGENTS.md sells it
as "the cheap early warning" for selectors_stale, and the agent loop leans on
it before committing to a run. An early warning structurally incapable of
seeing the most likely failure is worse than none: it turns "I don't know"
into a false "fine". No network.
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

# The two shapes a healthy search page is allowed to have.
SEARCH_SERVER_RENDERED = """
<div class="beer-item">
  <p class="name"><a href="/v/ghost-whale/1">Ghost Whale</a></p>
  <p class="style">Beer Bar</p>
  <p class="style">London, UK</p>
</div>
"""
SEARCH_CLIENT_RENDERED = '<div id="algolia-hits"></div>'
SEARCH_BROKEN = '<div id="who-knows"><p>Nothing we recognise</p></div>'


class _FakeClient:
    """Serves a venue page and a search page, without touching the network."""

    def __init__(self, venue_html: str, search_html: str):
        self._venue, self._search = venue_html, search_html
        self.urls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        self.urls.append(url)
        return self._search if "search" in url else self._venue


@pytest.fixture
def client(monkeypatch):
    def install(venue_html=VENUE_OK, search_html=SEARCH_SERVER_RENDERED):
        fake = _FakeClient(venue_html, search_html)
        monkeypatch.setattr(cli, "PoliteClient", lambda s: fake)
        return fake
    return install


@pytest.mark.unit
def test_selfcheck_probes_the_search_page_too(client):
    """The whole point: it has to look at the thing that broke."""
    fake = client()
    env = cli.cmd_selfcheck(Settings(), "ghost-whale", "1")
    assert env.ok, env.error
    assert any("search" in u for u in fake.urls), fake.urls


@pytest.mark.unit
def test_a_client_rendered_search_page_is_healthy(client):
    """An empty #algolia-hits is the normal HTTP response, not a break.

    Treating it as a failure would make selfcheck red on every healthy install
    since the Algolia change -- an alarm that is always on is also no alarm.
    """
    client(search_html=SEARCH_CLIENT_RENDERED)
    env = cli.cmd_selfcheck(Settings(), "ghost-whale", "1")
    assert env.ok, env.error
    assert env.data["search"] == "client-rendered"


@pytest.mark.unit
def test_a_search_page_of_neither_shape_is_reported_stale(client):
    """The case that went undetected for the length of a total outage."""
    client(search_html=SEARCH_BROKEN)
    env = cli.cmd_selfcheck(Settings(), "ghost-whale", "1")
    assert env.ok is False
    assert env.error.code == "selectors_stale"
    assert "search" in env.error.message.lower()


@pytest.mark.unit
def test_a_broken_venue_page_still_reports_first(client):
    """The existing check keeps precedence; the probe is additive."""
    client(venue_html="<div>nothing</div>", search_html=SEARCH_BROKEN)
    env = cli.cmd_selfcheck(Settings(), "ghost-whale", "1")
    assert env.ok is False
    assert env.error.code in {"selectors_stale", "stats_missing"}


@pytest.mark.unit
def test_skip_search_keeps_the_one_request_behaviour(client):
    """Documented as one request for years; leave that reachable."""
    fake = client(search_html=SEARCH_BROKEN)
    env = cli.cmd_selfcheck(Settings(), "ghost-whale", "1", probe_search=False)
    assert env.ok, env.error
    assert not any("search" in u for u in fake.urls)


@pytest.mark.unit
def test_the_probe_does_not_assert_on_result_count(client):
    """Anonymous search is capped at five by the login gate -- that is fine."""
    one_hit = SEARCH_SERVER_RENDERED
    client(search_html=one_hit)
    assert cli.cmd_selfcheck(Settings(), "ghost-whale", "1").ok
