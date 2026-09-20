"""Immutable value objects. Every transform returns a new instance."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class VenueRef:
    """What the search results page gives us."""

    venue_id: str
    slug: str
    name: str
    category: str | None
    address: str | None
    city: str | None

    @property
    def url(self) -> str:
        """The venue's page, or "" when this ref cannot name one.

        A ref rebuilt from a CSV that predates the `url` and `venue_id`
        columns has no slug, and interpolating one anyway produced
        `https://untappd.com/v//American Taproom - Waterloo`: a string that
        looks like a link, rides into the CSV and the KML's "View on Untappd",
        and fails only when somebody follows it. Absent beats confidently
        wrong, the same way a style line that cannot be placed is left empty.

        Deliberately not a search URL. A search is not this venue's page, and
        substituting one would be the same error in better clothes.
        """
        if not (self.slug and self.venue_id):
            return ""
        return f"https://untappd.com/v/{self.slug}/{self.venue_id}"


@dataclass(frozen=True)
class Venue:
    """A VenueRef enriched with stats and coordinates."""

    ref: VenueRef
    total: int | None
    unique: int | None
    monthly: int | None
    you: int | None
    lat: float | None = None
    lng: float | None = None
    geo_source: str = "none"  # embedded | google | nominatim | cache | none
    # Google Places `businessStatus`, or one of the two not-an-answer
    # states. "" means nobody asked; "unmatched" means Google was asked
    # and does not list this place. Neither is a closure -- see
    # `places.py` for why that distinction is the whole design.
    business_status: str = ""

    @property
    def has_public_stats(self) -> bool:
        return None not in (self.total, self.unique, self.monthly)

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lng is not None

    @property
    def is_closed(self) -> bool:
        """Only an explicit closed status from Places closes a venue.

        An allow-list, checked against `places.CLOSED_STATUSES`: no match,
        no key, a timeout, or a status Google adds next year all leave the
        venue visible. A false closure deletes a real bar from the map.
        """
        from .places import CLOSED_STATUSES

        return self.business_status in CLOSED_STATUSES

    def with_coords(self, lat: float, lng: float, source: str) -> Venue:
        return replace(self, lat=lat, lng=lng, geo_source=source)

    def with_business_status(self, status: str) -> Venue:
        return replace(self, business_status=status)

    def to_row(self) -> dict[str, Any]:
        r = self.ref
        return {
            "venue_id": r.venue_id,
            "name": r.name,
            "category": r.category or "",
            "address": r.address or "",
            "city": r.city or "",
            "total": self.total if self.total is not None else "",
            "unique": self.unique if self.unique is not None else "",
            "monthly": self.monthly if self.monthly is not None else "",
            "you": self.you if self.you is not None else "",
            "lat": f"{self.lat:.6f}" if self.lat is not None else "",
            "lng": f"{self.lng:.6f}" if self.lng is not None else "",
            "geo_source": self.geo_source,
            "business_status": self.business_status,
            "url": r.url,
        }


CSV_FIELDS = [
    "venue_id", "name", "category", "address", "city",
    "total", "unique", "monthly", "you",
    "lat", "lng", "geo_source", "business_status", "url",
]
