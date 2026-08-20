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

    @property
    def has_public_stats(self) -> bool:
        return None not in (self.total, self.unique, self.monthly)

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lng is not None

    def with_coords(self, lat: float, lng: float, source: str) -> Venue:
        return replace(self, lat=lat, lng=lng, geo_source=source)

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
            "url": r.url,
        }


CSV_FIELDS = [
    "venue_id", "name", "category", "address", "city",
    "total", "unique", "monthly", "you",
    "lat", "lng", "geo_source", "url",
]
