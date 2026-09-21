"""A finished sweep is placed again, not walked again; an old one is forgotten.

Measured 2026-09-22: a complete live Tel Aviv sweep failed at placement on an
Overpass 504, and the re-run walked every cell again, because a resumed
journal re-walks cells by design. And a journal that never expired carried
last week's venues into this week's sweep.

No emulator, no network.
"""
from __future__ import annotations

import os
import time

import pytest

from beer_in_this_town import app_pipeline, app_sweep
from beer_in_this_town.app_sweep import SweepResult, Venue

pytestmark = pytest.mark.unit

CITY = "Tel Aviv"
CENTRE = (32.08, 34.78)


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(app_sweep, "STATE_DIR", tmp_path)
    return tmp_path


def _result(*names: str) -> SweepResult:
    return SweepResult(venues=[Venue(n, 1, 2, 32.0, 34.0) for n in names],
                       cells_visited=len(names))


def _age(hours: float) -> None:
    path = app_sweep.journal_path(CITY)
    then = time.time() - hours * 3600
    os.utime(path, (then, then))


def test_an_interrupted_journal_is_not_finished():
    app_sweep.save_journal(CITY, _result("Lauter"))
    assert app_sweep.load_finished(CITY) is None


def test_a_completed_sweep_is_finished_with_its_centre():
    app_sweep.mark_complete(CITY, _result("Lauter", "Ursa"), CENTRE, "gps")
    done = app_sweep.load_finished(CITY)
    assert done is not None
    assert [v.name for v in done.result.venues] == ["Lauter", "Ursa"]
    assert done.centre == CENTRE and done.centre_source == "gps"


def test_a_finished_sweep_expires():
    app_sweep.mark_complete(CITY, _result("Lauter"), CENTRE, "geocoder")
    _age(app_sweep.JOURNAL_MAX_AGE_H + 1)
    assert app_sweep.load_finished(CITY) is None


def test_only_a_stale_journal_is_discarded_on_request():
    app_sweep.save_journal(CITY, _result("Lauter"))
    assert not app_sweep.discard_journal(CITY, only_if_older_than_h=12)
    _age(13)
    assert app_sweep.discard_journal(CITY, only_if_older_than_h=12)
    assert not app_sweep.journal_path(CITY).exists()


def _census(monkeypatch, *, fresh=False):
    """Run `census` with the device, the geocoder and OSM all faked, and
    report whether the map was walked."""
    walked = []

    def fake_sweep(*a, **kw):
        walked.append(True)
        return _result("Fresh Bar")

    monkeypatch.setattr(app_pipeline, "sweep", fake_sweep)
    monkeypatch.setattr(app_pipeline, "search_city", lambda *a, **kw: None)
    monkeypatch.setattr(app_pipeline, "calibrate",
                        lambda venues, known, centre: (venues, None))
    monkeypatch.setattr(app_pipeline, "to_venues", lambda r, city: r.venues)
    c = app_pipeline.census(object(), CITY, None, fresh=fresh,
                            locate_city=lambda city, s: CENTRE,
                            known_near=lambda centre, radius, s: [],
                            settle_min_s=0, settle_max_s=0)
    return walked, [v.name for v in c.venues]


def test_a_rerun_after_a_complete_sweep_does_not_walk_the_map(monkeypatch):
    app_sweep.mark_complete(CITY, _result("Lauter"), CENTRE, "geocoder")
    walked, names = _census(monkeypatch)
    assert walked == [] and names == ["Lauter"]


def test_fresh_walks_the_map_again(monkeypatch):
    app_sweep.mark_complete(CITY, _result("Lauter"), CENTRE, "geocoder")
    walked, names = _census(monkeypatch, fresh=True)
    assert walked == [True] and names == ["Fresh Bar"]


def test_a_new_sweep_is_marked_complete_before_placement(monkeypatch):
    """So a placement failure after it costs a re-placement, not a re-sweep."""
    def failing_calibrate(*a):
        raise RuntimeError("Overpass 504")

    monkeypatch.setattr(app_pipeline, "sweep", lambda *a, **kw: _result("X"))
    monkeypatch.setattr(app_pipeline, "search_city", lambda *a, **kw: None)
    monkeypatch.setattr(app_pipeline, "calibrate", failing_calibrate)
    with pytest.raises(RuntimeError):
        app_pipeline.census(object(), CITY, None,
                            locate_city=lambda city, s: CENTRE,
                            known_near=lambda centre, radius, s: [],
                            settle_min_s=0, settle_max_s=0)
    assert app_sweep.load_finished(CITY) is not None
