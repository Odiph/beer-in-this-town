"""Signed out, enrich must stop -- not return a map with no numbers.

Measured 2026-09-22 against a signed-out session, 7 Tel Aviv venues: the two
verified venues showed stats, the other four pages said "Log In to view Venue
Stats", and `enrich` reported them as `fetch_failed` layout changes inside an
`ok: true` envelope.

No network, no browser.
"""
from __future__ import annotations

import json

import pytest

from beer_in_this_town.config import ENRICHED_CSV, SWEEP_CSV, Settings, stage_path
from beer_in_this_town.export import write_csv
from beer_in_this_town.flow_cmds import cmd_enrich
from beer_in_this_town.models import Venue, VenueRef
from beer_in_this_town.parsers import StatsLoginRequired, parse_venue_stats

pytestmark = pytest.mark.unit

CITY = "Tel Aviv"

# The stats block exactly as Untappd served it to a signed-out session.
GATED = """<html><body><div class="venue-name"><h1>Oscar Wilde</h1></div>
<div class="stats">
  <h3>Venue Stats (<a href="javascript:void(0)" class="tip-info">?</a>)</h3>
  <div style="display: flex;">
    <a href="/login?go_to=https://untappd.com//v/oscar-wilde-irish-pub/4367301"
       style="margin-right: 4px;">Log In</a> to view Venue Stats
  </div>
</div></body></html>"""

REF = VenueRef(venue_id="4367301", slug="oscar-wilde-irish-pub",
               name="Oscar Wilde", category="Pub", address=None, city=CITY)


def test_the_login_prompt_is_named_for_what_it_is():
    with pytest.raises(StatsLoginRequired):
        parse_venue_stats(GATED, REF)


def test_the_login_prompt_writes_no_debug_page(tmp_path, monkeypatch):
    """A debug dump says "fix a selector"; this needs a sign-in instead."""
    from beer_in_this_town import parsers

    debug = tmp_path / "debug"
    monkeypatch.setattr(parsers, "DEBUG_DIR", debug)
    with pytest.raises(StatsLoginRequired):
        parse_venue_stats(GATED, REF)
    assert not debug.exists()


def _swept(name: str) -> Venue:
    return Venue(ref=VenueRef(venue_id="", slug="", name=name, category=None,
                              address=None, city=CITY),
                 total=None, unique=None, monthly=None, you=None,
                 lat=32.07, lng=34.78, geo_source="app")


def test_a_gated_page_stops_enrich_and_writes_nothing():
    write_csv([_swept("Oscar Wilde"), _swept("Lauter")],
              stage_path(CITY, SWEEP_CSV))
    fetched = []

    def fetch(ref):
        fetched.append(ref.name)
        raise StatsLoginRequired("gated")

    env = cmd_enrich(Settings(), CITY, search=lambda q: [REF], fetch=fetch)
    assert not env.ok
    assert env.error.code == "not_signed_in"
    assert fetched == ["Oscar Wilde"], "kept spending requests after the gate"
    assert not stage_path(CITY, ENRICHED_CSV).exists()
    assert env.next_actions == []   # a sign-in is the human's


def test_no_untappd_session_is_refused_before_any_request(tmp_path):
    state = tmp_path / "storage_state.json"
    state.write_text(json.dumps({"cookies": [
        {"name": "SID", "value": "x", "domain": ".google.com"}]}),
        encoding="utf-8")
    write_csv([_swept("Lauter")], stage_path(CITY, SWEEP_CSV))
    env = cmd_enrich(Settings(storage_state=state), CITY)
    assert env.error.code == "not_signed_in"
    assert "untappd" in env.error.remedy.lower()
