"""Enrich in batches, and never lose a looked-up venue.

Found live: a London enrich was stopped three times (the machine ran short
of memory), once at venue 391 of 629, and nothing had been written because
the CSV was written only at the end. The user asked for a batch system.

No network.
"""
from __future__ import annotations

import json

import pytest

from beer_in_this_town.config import Settings, stage_path
from beer_in_this_town.export import write_csv
from beer_in_this_town.flow_cmds import (
    ENRICHED_CSV,
    SWEEP_CSV,
    _journal_path,
    cmd_enrich,
)
from beer_in_this_town.models import Venue, VenueRef

pytestmark = pytest.mark.unit

CITY = "London, England"
S = Settings()
NAMES = [f"Tap {c}" for c in "ABCDEFG"]


def _swept(name: str) -> Venue:
    return Venue(ref=VenueRef(venue_id="", slug="", name=name, category=None,
                              address=None, city=CITY),
                 total=None, unique=None, monthly=None, you=None,
                 lat=51.5, lng=-0.12, geo_source="app")


def _page(name: str) -> Venue:
    ref = VenueRef(venue_id=str(NAMES.index(name) + 1), slug=name.lower(),
                   name=name, category="Pub", address="1 High St", city=CITY)
    return Venue(ref=ref, total=100, unique=50, monthly=5, you=None,
                 lat=51.5001, lng=-0.1201, geo_source="embedded")


class Web:
    def __init__(self):
        self.fetched: list[str] = []

    def search(self, q):
        return [_page(n).ref for n in NAMES if n.casefold() in q.casefold()]

    def fetch(self, ref):
        self.fetched.append(ref.name)
        return _page(ref.name)


@pytest.fixture
def web():
    write_csv([_swept(n) for n in NAMES], stage_path(CITY, SWEEP_CSV))
    return Web()


def _run(web, limit=None):
    return cmd_enrich(S, CITY, limit=limit, search=web.search, fetch=web.fetch)


def test_a_batch_stops_at_its_limit_and_says_what_is_left(web):
    env = _run(web, limit=3)
    assert env.ok
    assert env.data["done"] == 3 and env.data["remaining"] == 4
    assert env.data["csv"] is None
    assert env.next_actions == [
        'python -m beer_in_this_town enrich --city "London, England" '
        '--limit 3 --json']
    assert not stage_path(CITY, ENRICHED_CSV).exists(), \
        "filter must never see part of a city"


def test_batches_continue_without_looking_anything_up_twice(web):
    _run(web, limit=3)
    _run(web, limit=3)
    env = _run(web, limit=3)
    assert env.data["remaining"] == 0
    assert sorted(web.fetched) == sorted(NAMES), "a venue was fetched twice"
    assert env.next_actions == [
        'python -m beer_in_this_town filter --city "London, England" --json']
    assert env.data["rows"] == len(NAMES)
    assert stage_path(CITY, ENRICHED_CSV).exists()


def test_a_run_stopped_midway_keeps_what_it_did(web):
    def dies_on_the_fourth(ref):
        if ref.name == NAMES[3]:
            raise KeyboardInterrupt
        return web.fetch(ref)

    with pytest.raises(KeyboardInterrupt):
        cmd_enrich(S, CITY, search=web.search, fetch=dies_on_the_fourth)
    done = json.loads(_journal_path(CITY).read_text(encoding="utf-8"))["done"]
    assert sorted(done) == ["0", "1", "2"]

    env = _run(web)
    assert env.data["remaining"] == 0
    assert web.fetched.count(NAMES[0]) == 1


def test_a_new_sweep_starts_a_new_journal(web):
    _run(web, limit=3)
    write_csv([_swept(n) for n in NAMES[:2]], stage_path(CITY, SWEEP_CSV))
    env = _run(web)
    assert env.data["venues"] == 2 and env.data["done"] == 2


def test_limit_must_be_positive(capsys):
    from beer_in_this_town import cli

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["enrich", "--limit", "0"])
