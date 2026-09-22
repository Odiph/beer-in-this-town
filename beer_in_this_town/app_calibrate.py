"""Measure the map's scale on every run, from venues whose positions are known.

The camera converts pixels with an assumed metres-per-pixel, and the app
chooses its own zoom. Found live: after `Reset location` it landed on a
different zoom from a city search, and a sweep converted every pin at the old
10.2 m/px when the true figure was 8.1. Every venue came out about 600 m from
where it is, all in the same direction -- a map that looks entirely plausible.

So the scale is measured, not assumed. Swept venues are matched by name to a
source of real positions (OpenStreetMap, which needs no key), and a
similarity -- scale, rotation, shift -- is fitted from the matches and applied
to *every* venue, matched or not. Measured on that live sweep: 8.12 m/px,
-0.2 degrees, 40 m shift, and **18 m median error** afterwards, against
~600 m before.

The rotation and shift should come out near zero -- the camera centre is the
device GPS, good to ~24 m, and the map is north-up. They are fitted anyway,
because a fit that needs a large one is evidence that something upstream is
wrong, and that is checked rather than absorbed.

When the fit cannot be trusted, this refuses. Coordinates computed with a
guessed scale put every venue somewhere real and wrong, which is worse than
no coordinates: `write_kml` leaves an unlocated venue off the map, loudly.
"""
from __future__ import annotations

import math
import re
import statistics
import unicodedata
from dataclasses import dataclass, replace

from .app_sweep import Venue

# Fewer than this and any set of points can be fitted; the fit would be
# describing the matches rather than the map.
MIN_MATCHES = 4

# A residual worse than this multiple of the median is a mismatch, not noise
# -- usually a chain whose branch OSM does not list, or a marker the map
# nudged in a dense cluster (Lauter, 109 m). The floor stops a near-perfect
# fit from treating centimetres as outliers.
OUTLIER_FACTOR = 3.0
OUTLIER_FLOOR_M = 60.0

# What a fit may say before it is disbelieved. Pin positions are good to
# ~30 m, so a median residual far beyond that means the model is wrong; the
# map is north-up, so any real rotation means the matches are.
MAX_MEDIAN_RESIDUAL_M = 75.0
MAX_ROTATION_DEG = 3.0
SCALE_RANGE = (0.25, 4.0)

# Name fragments shorter than this match too much: "Bar", "Pub".
_MIN_KEY_LEN = 4
_SPLIT = re.compile(r"[()\[\]|/\-–—]")

_M_PER_DEG_LAT = 110_540.0
_M_PER_DEG_LNG_EQUATOR = 111_320.0


class CalibrationFailed(RuntimeError):
    """The scale could not be measured well enough to trust."""


@dataclass(frozen=True)
class Calibration:
    """What the fit found, kept so a run can report it."""

    scale: float              # multiply the assumed m/px by this
    rotation_deg: float
    shift_m: tuple[float, float]
    median_residual_m: float
    inliers: tuple[str, ...]
    dropped: tuple[str, ...]


def name_keys(name: str) -> set[str]:
    """Every comparable form of a venue name.

    Bilingual names are split, so `Agnes (אגנס)` matches an OSM entry named
    either way. Every script is kept: stripping to ASCII once collapsed all
    Hebrew names to the same empty string.
    """
    keys = set()
    for part in [*_SPLIT.split(name), name]:
        key = "".join(c for c in unicodedata.normalize("NFKC", part).casefold()
                      if c.isalnum())
        if len(key) >= _MIN_KEY_LEN:
            keys.add(key)
    return keys


def match_known(venues: list[Venue], known: list[tuple[str, float, float]]
                ) -> list[tuple[Venue, float, float]]:
    """Pair located venues with a known position, only where unambiguous.

    A name that matches more than one known place is dropped rather than
    guessed: a chain matched to the wrong branch was a 2 km outlier live.
    """
    index: dict[str, set[int]] = {}
    for i, (name, _, _) in enumerate(known):
        for key in name_keys(name):
            index.setdefault(key, set()).add(i)

    pairs = []
    for v in venues:
        if v.lat is None or v.lng is None:
            continue
        hits: set[int] = set()
        for key in name_keys(v.name):
            hits |= index.get(key, set())
        if len(hits) == 1:
            _, lat, lng = known[hits.pop()]
            pairs.append((v, lat, lng))
    return pairs


def _to_local(lat: float, lng: float, origin: tuple[float, float]) -> complex:
    """Metres east (real) and north (imaginary) of `origin`."""
    kx = _M_PER_DEG_LNG_EQUATOR * math.cos(math.radians(origin[0]))
    return complex((lng - origin[1]) * kx, (lat - origin[0]) * _M_PER_DEG_LAT)


def _from_local(p: complex, origin: tuple[float, float]) -> tuple[float, float]:
    kx = _M_PER_DEG_LNG_EQUATOR * math.cos(math.radians(origin[0]))
    return origin[0] + p.imag / _M_PER_DEG_LAT, origin[1] + p.real / kx


def _similarity(src: list[complex], dst: list[complex]
                ) -> tuple[complex, complex, list[float]]:
    """Least-squares `dst = a*src + b`, with `a` a complex scale-and-rotation."""
    ms, md = sum(src) / len(src), sum(dst) / len(dst)
    den = sum(abs(s - ms) ** 2 for s in src)
    if den == 0:
        raise CalibrationFailed("matched venues all sit at one point")
    a = sum((d - md) * (s - ms).conjugate() for s, d in zip(src, dst, strict=True)) / den
    b = md - a * ms
    return a, b, [abs(a * s + b - d) for s, d in zip(src, dst, strict=True)]


def _consensus(src: list[complex], dst: list[complex]) -> set[int]:
    """Indices of the largest set of matches that agree on one similarity.

    Agreement is within `OUTLIER_FLOOR_M`. Ties go to the smaller total
    error, so the result does not depend on the order of the matches.
    """
    best: tuple[int, float, set[int]] = (0, math.inf, set())
    n = len(src)
    for i in range(n):
        for j in range(i + 1, n):
            if src[i] == src[j]:
                continue
            a = (dst[j] - dst[i]) / (src[j] - src[i])
            b = dst[i] - a * src[i]
            errs = [abs(a * s + b - d) for s, d in zip(src, dst, strict=True)]
            agree = {k for k, e in enumerate(errs) if e <= OUTLIER_FLOOR_M}
            total = sum(errs[k] for k in agree)
            if (len(agree), -total) > (best[0], -best[1]):
                best = (len(agree), total, agree)
    return best[2] if best[2] else set(range(n))


@dataclass(frozen=True)
class CellAnchoring:
    """How each cell's position was settled before the global fit."""

    anchored: int          # cells shifted by their own OSM matches
    carried: int           # cells too sparse to measure; took the last shift
    unmeasured: int        # sparse cells before any measurement: left as-is
    max_shift_m: float     # the largest correction applied to any cell


# A cell needs this many unambiguous OSM matches to measure its own shift,
# and this share of them within AGREE_M of their median to trust it.
CELL_MIN_MATCHES = 3
CELL_AGREE_M = 250.0
CELL_AGREE_SHARE = 0.6


def anchor_cells(venues: list[Venue], known: list[tuple[str, float, float]],
                 origin: tuple[float, float]
                 ) -> tuple[list[Venue], CellAnchoring]:
    """Correct each cell's position from its own matches, before the fit.

    The camera is dead reckoning: it adds up measured pans. Found live on a
    London sweep: in dense areas the pan measurement is wrong often enough
    (pins matched across a re-queried result set) that the camera drifted,
    and from about a quarter of the way in every cell was placed 6-15 km off.
    One similarity cannot describe that -- the global fit kept the early
    cells and dropped 165 matches as outliers, and the drifted venues went
    out with their wrong coordinates.

    Every pin in one cell shares one camera position, so drift is a
    per-cell translation. Each cell with enough matches is shifted by the
    median offset to OpenStreetMap; a sparse cell takes the last measured
    shift, because drift persists until the next bad pan. Venues from
    sweeps that did not record cells are one group, which is the old
    behaviour.
    """
    cells: dict[int | None, list[int]] = {}
    for i, v in enumerate(venues):
        if v.lat is not None and v.lng is not None:
            cells.setdefault(v.cell, []).append(i)
    order = sorted(cells, key=lambda c: (c is None, c if c is not None else 0))

    out = list(venues)
    shift = 0j
    measured_once = False
    anchored = carried = unmeasured = 0
    max_shift = 0.0
    for cell in order:
        members = [venues[i] for i in cells[cell]]
        pairs = match_known(members, known)
        offsets = [_to_local(lat, lng, origin) - _to_local(v.lat, v.lng, origin)
                   for v, lat, lng in pairs]
        own = _agreed_offset(offsets)
        if own is not None:
            shift, measured_once = own, True
            anchored += 1
        elif measured_once:
            carried += 1
        else:
            unmeasured += 1
            continue
        max_shift = max(max_shift, abs(shift))
        for i in cells[cell]:
            v = venues[i]
            lat, lng = _from_local(_to_local(v.lat, v.lng, origin) + shift, origin)
            out[i] = replace(v, lat=lat, lng=lng)
    return out, CellAnchoring(anchored, carried, unmeasured, max_shift)


def _agreed_offset(offsets: list[complex]) -> complex | None:
    """The median offset, if enough of the offsets agree with it."""
    if len(offsets) < CELL_MIN_MATCHES:
        return None
    med = complex(statistics.median(o.real for o in offsets),
                  statistics.median(o.imag for o in offsets))
    agree = sum(1 for o in offsets if abs(o - med) <= CELL_AGREE_M)
    if agree < CELL_AGREE_SHARE * len(offsets):
        return None
    return med


def calibrate(venues: list[Venue], known: list[tuple[str, float, float]],
              origin: tuple[float, float]) -> tuple[list[Venue], Calibration]:
    """Fit the map's true scale and return every venue re-placed with it.

    `origin` is the camera's starting centre (the device GPS). Returns new
    venues; the input is not modified.
    """
    pairs = match_known(venues, known)
    if len(pairs) < MIN_MATCHES:
        raise CalibrationFailed(
            f"only {len(pairs)} venue(s) match a known position unambiguously; "
            f"{MIN_MATCHES} are needed to measure the scale.")

    src = [_to_local(v.lat, v.lng, origin) for v, _, _ in pairs]
    dst = [_to_local(lat, lng, origin) for _, lat, lng in pairs]
    names = [v.name for v, _, _ in pairs]
    dropped: list[str] = []

    # Consensus first, least squares second. A least-squares fit over every
    # match lets one bad one drag the model: a single 4 km mismatch among
    # seven pushed the median error to ~1 km, at which point the mismatch no
    # longer stood out and every good match looked bad. Two matches determine
    # a similarity exactly, so try every pair and keep the model the most
    # matches agree with. Match counts are small; this is cheap.
    keep = _consensus(src, dst)
    dropped.extend(n for i, n in enumerate(names) if i not in keep)
    src = [src[i] for i in keep]
    dst = [dst[i] for i in keep]
    names = [names[i] for i in keep]
    if len(src) < MIN_MATCHES:
        raise CalibrationFailed(
            f"only {len(src)} match(es) agree on one scale ({len(dropped)} "
            f"disagree); {MIN_MATCHES} are needed.")

    a, b, res = _similarity(src, dst)
    while len(src) > MIN_MATCHES:
        worst = max(range(len(res)), key=res.__getitem__)
        if res[worst] <= max(OUTLIER_FACTOR * statistics.median(res),
                             OUTLIER_FLOOR_M):
            break
        dropped.append(names.pop(worst))
        del src[worst], dst[worst]
        a, b, res = _similarity(src, dst)

    cal = Calibration(
        scale=abs(a),
        rotation_deg=math.degrees(math.atan2(a.imag, a.real)),
        shift_m=(b.real, b.imag),
        median_residual_m=statistics.median(res),
        inliers=tuple(names), dropped=tuple(dropped))

    problems = []
    if cal.median_residual_m > MAX_MEDIAN_RESIDUAL_M:
        problems.append(f"median error {cal.median_residual_m:.0f} m")
    if abs(cal.rotation_deg) > MAX_ROTATION_DEG:
        problems.append(f"rotation {cal.rotation_deg:.1f} deg on a north-up map")
    if not SCALE_RANGE[0] <= cal.scale <= SCALE_RANGE[1]:
        problems.append(f"scale factor {cal.scale:.2f}")
    if problems:
        raise CalibrationFailed(
            "the fit does not describe the map (" + ", ".join(problems)
            + f") across {len(names)} match(es); the matches are probably "
            "wrong, and no coordinates were changed.")

    fixed = []
    for v in venues:
        if v.lat is None or v.lng is None:
            fixed.append(v)
            continue
        lat, lng = _from_local(a * _to_local(v.lat, v.lng, origin) + b, origin)
        fixed.append(replace(v, lat=lat, lng=lng))
    return fixed, cal
