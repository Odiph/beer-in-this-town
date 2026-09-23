"""Which spellings of a city's name to search Untappd for.

Untappd takes its venues from Foursquare, and a venue's city line on
Untappd is Foursquare's `locality` for it -- in whatever language and
spelling the place was entered with. Tel Aviv's venues say "תל אביב-יפו",
"Tel Aviv", "Tel Aviv-Yafo", "Jaffa", ...; a search for one spelling misses
the others. So the spellings come from Foursquare's open places data
(FSQ OS Places, Apache 2.0): the localities of the places inside the
city's boundary, counted.

Two rules turn hundreds of spellings into a short list (the user's call,
2026-09-23: nothing under 2%, at most ten):

* A spelling whose words contain a shorter common spelling's words is
  folded into it. Untappd matches whole words, so one search for
  "tel aviv" also finds every "tel aviv yafo" line.
* What is left under 2% of the named places is dropped, and at most ten
  are kept, most places first.

The answer is cached per city. It is a property of the city, not of a run.
When the list cannot be built (no duckdb, no network, no boundary), the
city as typed is the only variant and the caller is told so -- a thinner
search, said out loud, rather than a stop.
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import CACHE_DIR, Settings, city_slug

log = logging.getLogger(__name__)

MIN_SHARE = 0.02
MAX_VARIANTS = 10
# A spelling rarer than this is a typo, not a name another can fold into.
MIN_COVER_SHARE = 0.005
CACHE_MAX_AGE_S = 90 * 24 * 3600

GAZETTEER_DIR = CACHE_DIR / "city_names"
FSQ_RELEASE = ("https://data.source.coop/fused/fsq-os-places/"
               "2025-02-06/places/")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

_DASHES = re.compile(r"[-‐-―/·・'’]+")


class NamesUnavailable(RuntimeError):
    """The variant list could not be built; the caller falls back."""


def norm(text: str) -> str:
    """A locality as Untappd's search sees it: case, dashes, country gone."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = t.split(",")[0]  # "Tel Aviv-Jaffa, Israel" -> the locality
    t = _DASHES.sub(" ", t)
    return " ".join(t.split())


@dataclass(frozen=True)
class Variant:
    query: str
    places: int
    share: float


def pick_variants(counts: dict[str, int], *, min_share: float = MIN_SHARE,
                  max_variants: int = MAX_VARIANTS) -> list[Variant]:
    """The spellings worth a search, most places first. See the docstring."""
    merged: dict[str, int] = {}
    for raw, n in counts.items():
        key = norm(raw)
        if key:
            merged[key] = merged.get(key, 0) + n
    total = sum(merged.values())
    if not total:
        return []
    covers = [k for k, n in merged.items() if n >= MIN_COVER_SHARE * total]
    folded: dict[str, int] = {}
    for key, n in merged.items():
        words = set(key.split())
        inside = [c for c in covers if c != key and set(c.split()) < words]
        head = (min(inside, key=lambda c: (len(c.split()), -merged[c]))
                if inside else key)
        folded[head] = folded.get(head, 0) + n
    kept = sorted(((k, n) for k, n in folded.items()
                   if n >= min_share * total), key=lambda kn: -kn[1])
    return [Variant(k, n, round(n / total, 4)) for k, n in kept[:max_variants]]


@dataclass(frozen=True)
class CityNames:
    city: str
    variants: tuple[str, ...]
    # (south, north, west, east), or None when the boundary is unknown.
    bbox: tuple[float, float, float, float] | None = None
    source: str = ""
    warning: str = ""

    def contains(self, lat: float, lng: float) -> bool:
        """Inside the city's bounding box. Unknown box: everything is."""
        if self.bbox is None:
            return True
        s, n, w, e = self.bbox
        return s <= lat <= n and w <= lng <= e


def _cache_path(city: str) -> Path:
    return GAZETTEER_DIR / f"{city_slug(city)}.json"


def _load(city: str) -> CityNames | None:
    path = _cache_path(city)
    try:
        if time.time() - path.stat().st_mtime > CACHE_MAX_AGE_S:
            return None
        rec = json.loads(path.read_text(encoding="utf-8"))
        bbox = rec.get("bbox")
        return CityNames(city=rec["city"], variants=tuple(rec["variants"]),
                         bbox=tuple(bbox) if bbox else None,
                         source=rec.get("source", ""))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def cached(city: str) -> CityNames | None:
    """The city's variants and bounds if already looked up; never fetches."""
    return _load(city)


def for_city(city: str, settings: Settings | None) -> CityNames:
    """The city's variants: cached, else built, else the city as typed."""
    known = _load(city)
    if known is not None:
        return known
    try:
        names = _build(city, settings)
    except NamesUnavailable as exc:
        log.warning("City name variants unavailable (%s); searching %r only.",
                    exc, city)
        return CityNames(city=city, variants=(norm(city),),
                         warning=f"Searched only {city!r}: the spellings of "
                                 f"its name could not be looked up ({exc}). "
                                 f"Venues listed under another spelling "
                                 f"were not searched.")
    path = _cache_path(city)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "city": names.city, "variants": list(names.variants),
        "bbox": list(names.bbox) if names.bbox else None,
        "source": names.source, "at": time.time(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return names


# --- building the list: needs the network and the `search` extra ----------

def _build(city: str, settings: Settings | None) -> CityNames:
    """Boundary from Nominatim, localities from Foursquare, counted."""
    try:
        import duckdb  # noqa: F401
        import pyarrow.parquet  # noqa: F401
    except ImportError as exc:
        raise NamesUnavailable(
            f"{exc.name} is not installed; pip install -e \".[search]\""
        ) from exc
    hit = _boundary(city, settings)
    bbox = tuple(float(x) for x in hit["boundingbox"])  # s, n, w, e
    counts = _locality_counts(hit, bbox)
    picked = pick_variants(counts)
    if not picked:
        raise NamesUnavailable(f"Foursquare has no named places in {city!r}")
    log.info("%s: %s", city, ", ".join(f"{v.query} ({v.share:.0%})"
                                       for v in picked))
    return CityNames(city=city, variants=tuple(v.query for v in picked),
                     bbox=bbox, source=f"fsq-os-places {FSQ_RELEASE}")


def _boundary(city: str, settings: Settings | None) -> dict:  # pragma: no cover
    import httpx

    from .geocode import _user_agent

    s = settings or Settings()
    try:
        r = httpx.get(NOMINATIM_URL, timeout=30,
                      params={"q": city, "format": "jsonv2", "limit": 5,
                              "polygon_geojson": 1},
                      headers={"User-Agent": _user_agent(s)})
        r.raise_for_status()
        hits = r.json()
    except Exception as exc:
        raise NamesUnavailable(f"the boundary lookup failed: {exc}") from exc
    for hit in hits:
        if (hit.get("geojson") or {}).get("type") in ("Polygon", "MultiPolygon"):
            return hit
    raise NamesUnavailable(f"no boundary for {city!r}")


def _locality_counts(hit: dict, bbox) -> dict[str, int]:  # pragma: no cover
    """Places per locality inside the boundary, read remotely.

    Only the row groups whose coordinate statistics overlap the box are
    read, three columns of them: a few MB for a city, not the 100M places.
    """
    import duckdb
    import httpx
    import pyarrow.parquet as pq

    south, north, west, east = bbox
    meta = CACHE_DIR / "fsq" / "_metadata"
    try:
        if not meta.exists():
            meta.parent.mkdir(parents=True, exist_ok=True)
            r = httpx.get(FSQ_RELEASE + "_metadata", timeout=120)
            r.raise_for_status()
            meta.write_bytes(r.content)
        md = pq.read_metadata(meta)
        names = md.schema.names
        li, lo = names.index("latitude"), names.index("longitude")
        files = set()
        for i in range(md.num_row_groups):
            rg = md.row_group(i)
            a, b = rg.column(li).statistics, rg.column(lo).statistics
            if (a is not None and b is not None and a.has_min_max
                    and a.min <= north and a.max >= south
                    and b.min <= east and b.max >= west):
                files.add(rg.column(0).file_path)
        if not files:
            return {}
        con = duckdb.connect()
        con.sql("install httpfs; load httpfs; install spatial; load spatial;")
        shape = json.dumps(hit["geojson"]).replace("'", "''")
        urls = ", ".join(f"'{FSQ_RELEASE}{f}'" for f in sorted(files))
        rows = con.sql(f"""
            select locality, count(*) from read_parquet([{urls}])
            where locality is not null
              and latitude between {south} and {north}
              and longitude between {west} and {east}
              and ST_Contains(ST_GeomFromGeoJSON('{shape}'),
                              ST_Point(longitude, latitude))
            group by 1""").fetchall()
    except NamesUnavailable:
        raise
    except Exception as exc:
        raise NamesUnavailable(f"Foursquare's places could not be read: "
                               f"{exc}") from exc
    return {loc: n for loc, n in rows}
