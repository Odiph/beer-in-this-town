"""Drift is per cell; so is the correction.

Found live, London 2026-09-22: the camera's dead reckoning went wrong a
quarter of the way into the sweep and every later cell was placed 6-15 km
off. The global fit kept the early cells, dropped 165 matches as outliers,
and exported the drifted venues with their wrong coordinates -- enrich then
refused 224 of 391 as more than 1 km from their own venue page.

No device, no network.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.app_calibrate import (
    _from_local,
    _to_local,
    anchor_cells,
    calibrate,
)
from beer_in_this_town.app_sweep import Venue
from beer_in_this_town.geo import haversine_km

pytestmark = pytest.mark.unit

ORIGIN = (51.5074, -0.1278)
DRIFT = complex(-6000, 9000)     # metres east, north: London's kind of error


def _at(east: float, north: float) -> tuple[float, float]:
    return _from_local(complex(east, north), ORIGIN)


def _city(cells: int = 6, per_cell: int = 5, drift_from: int = 3,
          sparse: frozenset[int] = frozenset()):
    """Venues whose true positions are known, with cells >= drift_from placed
    DRIFT away from the truth. A sparse cell's venues are not in OSM."""
    known, swept, truth = [], [], {}
    for cell in range(1, cells + 1):
        for k in range(per_cell):
            # Distinct letters, no separators: `name_keys` splits on "-" and
            # would make "Pub 1-0" and "Pub 1-1" the same key.
            name = f"The {'ABCDEFGH'[cell]}{'PQRSTUVW'[k]}x Tavern"
            east, north = cell * 900.0 + k * 120, k * 150.0 - 300
            true = _at(east, north)
            truth[name] = true
            if cell not in sparse:
                known.append((name, *true))
            off = DRIFT if cell >= drift_from else 0
            lat, lng = _from_local(_to_local(*true, ORIGIN) + off, ORIGIN)
            swept.append(Venue(name, 0, 0, lat, lng, cell=cell))
    return swept, known, truth


def _errors_m(venues, truth):
    return [haversine_km((v.lat, v.lng), truth[v.name]) * 1000 for v in venues]


def test_drifted_cells_are_put_back():
    swept, known, truth = _city()
    anchored, report = anchor_cells(swept, known, ORIGIN)
    assert max(_errors_m(anchored, truth)) < 1.0
    assert report.anchored == 6 and report.carried == 0
    assert report.max_shift_m == pytest.approx(abs(DRIFT), rel=1e-3)


def test_the_global_fit_alone_leaves_the_drift_in():
    """What shipped in 0.2.0: the fit keeps the early cells and places the
    rest where the camera thought they were."""
    swept, known, truth = _city(drift_from=5)
    fixed, _ = calibrate(swept, known, ORIGIN)
    assert max(_errors_m(fixed, truth)) > 5000


def test_anchoring_then_the_fit_is_accurate():
    swept, known, truth = _city()
    anchored, _ = anchor_cells(swept, known, ORIGIN)
    fixed, cal = calibrate(anchored, known, ORIGIN)
    assert max(_errors_m(fixed, truth)) < 1.0
    assert not cal.dropped


def test_a_sparse_cell_carries_the_last_measured_shift():
    swept, known, truth = _city(sparse=frozenset({4}))
    anchored, report = anchor_cells(swept, known, ORIGIN)
    cell4 = [v for v in anchored if v.cell == 4]
    assert max(_errors_m(cell4, truth)) < 1.0
    assert report.carried == 1


def test_cells_that_disagree_among_themselves_are_not_trusted():
    """Matches all over the place are not a measurement."""
    swept, known, truth = _city(cells=1, per_cell=5, drift_from=99)
    scattered = [(n, lat + i * 0.02, lng) for i, (n, lat, lng) in
                 enumerate(known)]
    anchored, report = anchor_cells(swept, scattered, ORIGIN)
    assert report.anchored == 0 and report.unmeasured == 1
    assert anchored == swept


def test_a_journal_without_cells_is_one_group():
    swept, known, truth = _city(drift_from=99)
    old = [Venue(v.name, v.x, v.y, v.lat, v.lng) for v in swept]
    anchored, report = anchor_cells(old, known, ORIGIN)
    assert report.anchored == 1
    assert max(_errors_m(anchored, truth)) < 1.0
