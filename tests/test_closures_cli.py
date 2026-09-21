"""The closure check, tested where this repo's bugs actually live.

`test_places.py` covers the rule. This file covers the layer above it, because
every one of the five swallowed-exception bugs in this project's history was a
correct raise reaching a caller that dropped it. So: does `PlacesUnavailable`
reach the envelope as `places_unavailable`, or does it surface as
`unexpected_error` with `ok: true` nowhere in sight?

Also here: the things a command must refuse to do. Check the key before
scraping, not after. Never overwrite the input CSV. Never put an account write
in `next_actions`.

No network.
"""
from __future__ import annotations

import csv

import pytest

from beer_in_this_town import cli, places
from beer_in_this_town.cli import build_parser, cmd_closures, cmd_run
from beer_in_this_town.config import Settings
from beer_in_this_town.export import write_csv
from beer_in_this_town.models import Venue, VenueRef
from beer_in_this_town.places import PlacesUnavailable


def _venue(vid: str, name: str) -> Venue:
    return Venue(
        ref=VenueRef(venue_id=vid, slug=name.lower().replace(" ", "-"), name=name,
                     category="Beer Bar", address=f"{vid} Some Road", city="London"),
        total=400, unique=90, monthly=0, you=None, lat=51.5, lng=-0.1,
    )


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "venues_london.csv"
    write_csv([_venue(str(i), f"Bar {i}") for i in range(1, 6)], path)
    return path


def _keyed() -> Settings:
    return Settings(google_places_key="test-key")


def _rows(path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# --- the swallow, at the caller ------------------------------------------

@pytest.mark.unit
def test_a_dead_key_reaches_the_envelope_as_places_unavailable(corpus, monkeypatch):
    """Not `unexpected_error`, and certainly not a CSV of `unmatched`."""
    def denied(venues, s):
        raise PlacesUnavailable("HTTP 403: Places API has not been used")

    monkeypatch.setattr(cli, "resolve_closures", denied)
    env = cmd_closures(_keyed(), str(corpus), None, None)

    assert env.ok is False
    assert env.error.code == "places_unavailable"
    assert "GOOGLE_PLACES_KEY" in env.error.remedy


@pytest.mark.unit
def test_a_failed_check_writes_no_csv(corpus, tmp_path, monkeypatch):
    """Nothing written, same posture as the corpus gate and the geocoder."""
    def denied(venues, s):
        raise PlacesUnavailable("HTTP 429: quota exceeded")

    monkeypatch.setattr(cli, "resolve_closures", denied)
    out = tmp_path / "checked.csv"
    cmd_closures(_keyed(), str(corpus), str(out), None)
    assert not out.exists()


# --- asked for, and not possible -----------------------------------------

@pytest.mark.unit
def test_no_key_fails_loudly_rather_than_no_opping(corpus):
    """The stage no-ops when nobody asked. This is somebody asking.

    Silently doing nothing in reply to an explicit command is how a user
    concludes every venue is open.
    """
    env = cmd_closures(Settings(), str(corpus), None, None)
    assert env.ok is False
    assert env.error.code == "places_key_missing"


@pytest.mark.unit
def test_run_checks_the_key_before_it_scrapes_anything(monkeypatch):
    """A hundred requests, then "no key", is the wrong order to find out."""
    scraped = []
    monkeypatch.setattr(cli, "collect_venue_refs",
                        lambda *a, **kw: scraped.append(1) or [])

    env = cmd_run(Settings(), upload=False, force_browser=False,
                  skip_robots=True, check_closed=True)

    assert env.ok is False
    assert env.error.code == "places_key_missing"
    assert scraped == [], "run scraped before checking it could do the job"


# --- what it does to the data --------------------------------------------

@pytest.mark.unit
def test_closed_venues_are_flagged_and_kept(corpus, monkeypatch):
    """Flagging is the capability. Dropping would be a decision, and it is
    the user's -- `run` uploads to My Maps, so a silent drop is invisible."""
    def shut(venues, s):
        return [v.with_business_status("CLOSED_PERMANENTLY") if v.ref.venue_id == "2"
                else v.with_business_status("OPERATIONAL") for v in venues]

    monkeypatch.setattr(cli, "resolve_closures", shut)
    env = cmd_closures(_keyed(), str(corpus), None, None)

    assert env.ok is True
    assert env.data["closed"] == 1
    assert env.data["venues"] == 5, "a flagged venue was dropped, not flagged"

    rows = _rows(__import__("pathlib").Path(env.data["csv"]))
    assert len(rows) == 5
    assert [r["business_status"] for r in rows].count("CLOSED_PERMANENTLY") == 1


@pytest.mark.unit
def test_the_input_csv_is_never_overwritten(corpus, monkeypatch):
    """A corpus costs a hundred requests. A billed stage must not clobber it."""
    before = corpus.read_bytes()
    monkeypatch.setattr(cli, "resolve_closures",
                        lambda venues, s: [v.with_business_status("OPERATIONAL")
                                           for v in venues])
    env = cmd_closures(_keyed(), str(corpus), None, None)

    assert corpus.read_bytes() == before
    assert env.data["csv"] != str(corpus)


@pytest.mark.unit
def test_a_limited_trial_still_writes_every_row(corpus, monkeypatch):
    """A --limit run produces a complete CSV, not a truncated corpus.

    Writing only the checked rows would turn a trial into data loss, since
    the annotated file is the one a user carries forward.
    """
    monkeypatch.setattr(cli, "resolve_closures",
                        lambda venues, s: [v.with_business_status("OPERATIONAL")
                                           for v in venues])
    env = cmd_closures(_keyed(), str(corpus), None, 2)

    rows = _rows(__import__("pathlib").Path(env.data["csv"]))
    assert len(rows) == 5
    assert env.data["checked"] == 2
    assert sum(1 for r in rows if r["business_status"] == "OPERATIONAL") == 2
    assert sum(1 for r in rows if r["business_status"] == "") == 3
    assert any("Trial run" in w for w in env.warnings)


@pytest.mark.unit
def test_unmatched_venues_are_warned_about_in_those_words(corpus, monkeypatch):
    """The envelope has to say `unmatched` is not `closed`, or a reader
    counting non-OPERATIONAL rows will conclude otherwise."""
    monkeypatch.setattr(cli, "resolve_closures",
                        lambda venues, s: [v.with_business_status(places.UNMATCHED)
                                           for v in venues])
    env = cmd_closures(_keyed(), str(corpus), None, None)

    assert env.data["closed"] == 0
    assert any("NOT as closed" in w for w in env.warnings)


# --- the agent contract ---------------------------------------------------

@pytest.mark.unit
def test_closures_never_suggests_an_account_write(corpus, monkeypatch):
    """`pin` and `notes` stay out of next_actions. AGENTS.md rule 2."""
    monkeypatch.setattr(cli, "resolve_closures",
                        lambda venues, s: [v.with_business_status("OPERATIONAL")
                                           for v in venues])
    env = cmd_closures(_keyed(), str(corpus), None, None)
    joined = " ".join(env.next_actions)
    assert "pin" not in joined and "notes" not in joined


@pytest.mark.unit
def test_closures_is_a_real_subcommand_with_a_limit(corpus):
    """Parsed, not just implemented. A command absent from the parser is a
    function nobody can call."""
    args = build_parser().parse_args(
        ["closures", "--csv", str(corpus), "--limit", "3", "--json"])
    assert args.cmd == "closures"
    assert args.limit == 3
    assert args.json is True


@pytest.mark.unit
def test_run_takes_check_closed_and_defaults_to_off(corpus):
    """Off by default: `run` uploads to My Maps, so a new stage does not get
    to change what lands there without being asked."""
    assert build_parser().parse_args(["run"]).check_closed is False
    assert build_parser().parse_args(["run", "--check-closed"]).check_closed is True


@pytest.mark.unit
def test_an_escaped_places_failure_never_advises_a_retry(corpus, monkeypatch, capsys):
    """`PlacesUnavailable` subclasses RuntimeError, so the fall-through in
    `main` would answer "re-run with -v" -- the one remedy that cannot help a
    rejected key. #20 adds callers to that module; the net is for them.
    """
    def escaped(*a, **kw):
        raise PlacesUnavailable("HTTP 403: API key not valid")

    monkeypatch.setattr(cli, "cmd_closures", escaped)
    code = cli.main(["closures", "--csv", str(corpus)])

    assert code == 1
    out = capsys.readouterr().out
    assert "unexpected" not in out.lower()
    assert "GOOGLE_PLACES_KEY" in out


@pytest.mark.unit
def test_the_annotated_csv_keeps_its_coordinates(corpus, monkeypatch):
    """A round trip through `closures` must not blank the geocoding.

    It did: `venues_from_csv` dropped lat/lng because a labelling pass does
    not read them, which was true until a caller wrote a full CSV back out.
    A KML built from the result would have had no pins at all -- and the
    envelope would still have said ok.
    """
    monkeypatch.setattr(cli, "resolve_closures",
                        lambda venues, s: [v.with_business_status("OPERATIONAL")
                                           for v in venues])
    env = cmd_closures(_keyed(), str(corpus), None, None)

    rows = _rows(__import__("pathlib").Path(env.data["csv"]))
    assert all(r["lat"] and r["lng"] for r in rows), "coordinates were dropped"
    assert all(float(r["lat"]) == 51.5 for r in rows)
