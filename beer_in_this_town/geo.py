"""How far a venue is from where you asked about.

Untappd's search matches venue *names*, not geography. Searching "Tel Aviv"
returns `Tel Aviv Grill` in Encino, California -- 12,137 km away -- along with
a Miami Beach kitchen and a cafe in New Jersey. On the first live run, 21 of
100 results were more than 100 km from Tel Aviv, and because `--count` caps
the total, those 21 crowded out 21 real Tel Aviv bars.

Nothing in the pipeline could tell. Every venue carries coordinates, and no
code had ever compared them to anything.

So: a distance, and a filter built on it. The filter's one rule is the one
this project keeps arriving at from different directions -- **a venue whose
distance cannot be established is kept, not dropped.** No coordinates means
unknown, and dropping on unknown is how a correct-looking corpus quietly
loses real venues. Being wrong the other way costs one row somebody can see
and delete.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Venue

EARTH_RADIUS_KM = 6371.0088  # mean radius, IUGG


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in kilometres between two (lat, lng) points.

    Haversine rather than a flat approximation: the error of pretending the
    world is flat is small at city scale and absurd at the scale this exists
    to catch, and the whole point is telling 3 km from 12,000 km.
    """
    lat1, lng1 = a
    lat2, lng2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def distance_from(centre: tuple[float, float], v: Venue) -> float | None:
    """How far this venue is from the centre, or None when it cannot be said."""
    if not v.has_coords:
        return None
    return haversine_km(centre, (v.lat, v.lng))


@dataclass(frozen=True)
class RadiusResult:
    """What a radius filter kept, dropped, and could not judge."""

    kept: tuple[Venue, ...]
    dropped: tuple[tuple[Venue, float], ...]   # venue and how far out it was
    unplaceable: tuple[Venue, ...]             # no coordinates: kept, not cut
    furthest_kept_km: float = 0.0

    @property
    def summary(self) -> dict:
        return {
            "kept": len(self.kept),
            "dropped": len(self.dropped),
            "unplaceable": len(self.unplaceable),
            "furthest_kept_km": round(self.furthest_kept_km, 1),
        }

    def worst_offenders(self, n: int = 5) -> list[tuple[str, float]]:
        """The most distant things that were cut, for saying so out loud."""
        ordered = sorted(self.dropped, key=lambda pair: -pair[1])[:n]
        return [(v.ref.name, round(km, 0)) for v, km in ordered]


def within_radius(venues: list[Venue], centre: tuple[float, float],
                  radius_km: float) -> RadiusResult:
    """Split venues by distance from a centre.

    Returns rather than filters, because the caller has to be able to *say*
    what went: a filter that silently halves a corpus is indistinguishable
    from a scrape that went wrong, and this project's whole posture is that
    a number nobody can account for is worse than no number.
    """
    if radius_km <= 0:
        raise ValueError("A radius has to be greater than zero.")

    kept: list[Venue] = []
    dropped: list[tuple[Venue, float]] = []
    unplaceable: list[Venue] = []
    furthest = 0.0

    for v in venues:
        km = distance_from(centre, v)
        if km is None:
            # Unknown, not far. Keeping it is the same call `unmatched` makes
            # in the closure check and `None` makes in the session probes.
            unplaceable.append(v)
            kept.append(v)
            continue
        if km <= radius_km:
            kept.append(v)
            furthest = max(furthest, km)
        else:
            dropped.append((v, km))

    return RadiusResult(tuple(kept), tuple(dropped), tuple(unplaceable),
                        furthest)


def parse_radius(raw: str) -> float:
    """`15`, `15km`, `9mi` -> kilometres. Refuses anything it cannot read.

    Guessing a unit is how somebody asks for 9 miles and gets 9 kilometres,
    which looks plausible and is wrong by a third.
    """
    text = (raw or "").strip().lower().replace(" ", "")
    if not text:
        raise ValueError("A radius is needed, e.g. 15km or 9mi.")

    factor = 1.0
    for suffix, scale in (("km", 1.0), ("mi", 1.609344), ("m", 0.001)):
        if text.endswith(suffix):
            text, factor = text[: -len(suffix)], scale
            break

    try:
        value = float(text)
    except ValueError:
        raise ValueError(
            f"Could not read {raw!r} as a distance. Use e.g. 15km, 9mi or 15."
        ) from None
    if value <= 0:
        raise ValueError("A radius has to be greater than zero.")
    return value * factor
