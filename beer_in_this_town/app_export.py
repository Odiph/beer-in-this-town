"""A map sweep, as the rows every exporter already understands.

The app sweep is the geographic census: it knows a venue's name and where it
is. The web venue page knows the rest -- the Untappd id, the category and the
check-in counts. Joining the two is a separate step; this module is what makes
a sweep useful before that step exists, so its output can go straight onto a
map.

Everything the sweep does not know travels as `None`, which the exporters
render as unknown. Never as `0`: a bar with no counts yet is not a bar nobody
visits, and ranking on a fabricated zero would bury it.
"""
from __future__ import annotations

from .app_sweep import SweepResult
from .models import Venue, VenueRef

# How a reader tells a map-pin fit (about 30 m median) from coordinates the
# venue page published itself (`embedded`).
GEO_SOURCE = "app"


def to_venues(result: SweepResult, city: str) -> list[Venue]:
    return [
        Venue(
            ref=VenueRef(venue_id="", slug="", name=v.name, category=None,
                         address=None, city=city),
            total=None, unique=None, monthly=None, you=None,
            lat=v.lat, lng=v.lng,
            geo_source=GEO_SOURCE if v.lat is not None else "none",
        )
        for v in result.venues
    ]
