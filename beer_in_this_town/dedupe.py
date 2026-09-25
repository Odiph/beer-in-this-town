"""The same bar under several Untappd ids (#9): flagged, never merged.

Untappd lets anyone create a venue, so a bar can exist as the owner's entry,
a customer's second try, a typo and a pre-rename name -- each with its own id
and its own share of the check-ins. Two signals say two rows are one place:

* the same Foursquare place id on both pages -- certain, whatever the names;
* the same normalised name within RADIUS_M -- probable. Never the name
  alone: two branches of a chain share one, and Ghost Whale Brixton and
  Putney are both real.

The quieter entry points at the busiest one in its group (`duplicate_of`).
Nothing is dropped and nothing is summed: whether the entries split one
venue's traffic cannot be established from the data, and a wrong merge
deletes a real bar, where a missed one costs a duplicate pin.
"""
from __future__ import annotations

import math
import re
import unicodedata

RADIUS_M = 100.0

_SUFFIX = re.compile(r"\s*[(\[].*$|\s[-–|]\s.*$")
_THE = re.compile(r"^the\s+")
_NON_ALNUM = re.compile(r"[\W_]+")


def name_key(name: str | None) -> str:
    """A name as a duplicate would type it: accents, case, "The", branch
    suffixes ("- Brixton", "(Wetherspoon)", a translation in brackets) and
    punctuation all dropped."""
    t = unicodedata.normalize("NFKD", name or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    t = _SUFFIX.sub("", t).strip()
    t = _THE.sub("", t)
    return _NON_ALNUM.sub("", t)


def _coords(row: dict) -> tuple[float, float] | None:
    try:
        return float(row.get("lat") or ""), float(row.get("lng") or "")
    except ValueError:
        return None


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    la, lo, lb, lob = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lb - la) / 2) ** 2
         + math.cos(la) * math.cos(lb) * math.sin((lob - lo) / 2) ** 2)
    return 6_371_000 * 2 * math.asin(math.sqrt(h))


def _total(row: dict) -> int:
    raw = str(row.get("total") or "").replace(",", "")
    return int(raw) if raw.isdigit() else -1


def possible_duplicates(rows: list[dict], radius_m: float = RADIUS_M
                        ) -> dict[str, str]:
    """{venue_id: venue_id of the busiest entry in its group} for every
    entry that is not the busiest. Rows without a venue id are skipped."""
    rows = [r for r in rows if r.get("venue_id")]
    parent = {r["venue_id"]: r["venue_id"] for r in rows}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    by_fsq: dict[str, str] = {}
    by_name: dict[str, list[tuple[str, tuple[float, float]]]] = {}
    for r in rows:
        vid = r["venue_id"]
        fsq = (r.get("fsq_id") or "").strip()
        if fsq:
            if fsq in by_fsq:
                join(vid, by_fsq[fsq])
            else:
                by_fsq[fsq] = vid
        key, here = name_key(r.get("name")), _coords(r)
        if not key or here is None:
            continue
        for other, there in by_name.setdefault(key, []):
            if _metres(here, there) <= radius_m:
                join(vid, other)
        by_name[key].append((vid, here))

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(find(r["venue_id"]), []).append(r)
    out: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        busiest = max(members, key=_total)["venue_id"]
        out.update({m["venue_id"]: busiest for m in members
                    if m["venue_id"] != busiest})
    return out
