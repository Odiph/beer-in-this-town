"""Measure a candidate classifier against human labels, honestly.

`classify.py` is a pile of unvalidated thresholds. This module is how they stop
being guesses: sample venues, have a human label them, and report how often the
classifier was wrong -- and in which direction, because the two directions do
not cost the same.

Why not sample at random
------------------------
Most venues in a city scrape are ordinary bars. A random 100 might contain
three private spaces, which is nowhere near enough to estimate the rate that
matters most. So this takes a fixed quota from every bucket the classifier
produces, including the rare ones, and then divides each bucket's contribution
back down by how heavily it was sampled.

That reweighting is load-bearing. Ten private venues in a thousand is 1%; a
quota sample that takes ten of each makes them 50% of the sheet. Reporting the
raw sample rate would declare a catastrophe on a corpus that is 99% fine, and
every threshold tuned against it would be wrong in the same direction.

The one rule for whoever labels: do not hand-pick which rows to fill in. The
weights assume the labelled rows are representative *within* their bucket.
`score_labels` recomputes weights from what was actually labelled and complains
when a bucket is thin, but it cannot detect cherry-picking, only its shadow.
"""
from __future__ import annotations

import csv
import json
import logging
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .classify import Kind, Verdict, classify
from .models import Venue, VenueRef

log = logging.getLogger(__name__)

# Columns the human fills in. Three, deliberately: every extra one multiplies
# the time cost across every sampled row.
ANSWER_FIELDS = ("is_public", "is_open", "true_kind")

SHEET_FIELDS = (
    "venue_id", "name", "category", "address", "city",
    "total", "unique", "monthly", "url",
    *ANSWER_FIELDS,
    "predicted_kind", "predicted_verdict", "reason", "_stratum",
)

DEFAULT_QUOTA = 18  # ~7 buckets -> ~125 rows -> ~45 minutes, once.

# Carried in the sheet itself: separated from its bucket sizes, a set of
# answers cannot be weighted and is worth nothing.
SIZES_HEADER = "beer-in-this-town bucket sizes: "

# Below this many labelled rows a bucket's weight is doing more work than the
# evidence supports. Not fatal -- the estimate is still the best available --
# but it must be said out loud.
THIN_STRATUM = 5


class PartialStratum(RuntimeError):
    """A sampled bucket came back with no labels at all.

    Its venues still count toward the population, so dropping it silently
    would understate exactly the bucket most likely to have been skipped as
    tedious -- and the tedious buckets are the junk ones.
    """


@dataclass(frozen=True)
class LabelSheet:
    """Rows for a human to fill in, plus what scoring needs to reweight them."""

    rows: list[dict[str, Any]]
    stratum_sizes: dict[str, int] = field(default_factory=dict)

    def counts_by_stratum(self) -> dict[str, int]:
        return dict(Counter(r["_stratum"] for r in self.rows))


def _yes(value: Any) -> bool:
    return str(value).strip().lower() in {"y", "yes", "true", "1"}


def _answered(row: dict[str, Any]) -> bool:
    return any(str(row.get(f, "")).strip() for f in ANSWER_FIELDS)


def stratified_sample(
    venues: list[Venue], quota: int = DEFAULT_QUOTA, seed: int = 0
) -> LabelSheet:
    """Take up to `quota` venues from each bucket the classifier produces.

    Seeded, so a half-finished sheet regenerates identically rather than
    handing the labeller a different hundred rows.
    """
    strata: dict[str, list[tuple[Venue, Any]]] = defaultdict(list)
    for v in venues:
        c = classify(v)
        strata[c.bucket].append((v, c))

    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    sizes: dict[str, int] = {}

    for bucket in sorted(strata):
        members = strata[bucket]
        sizes[bucket] = len(members)
        # Sort before sampling: dict order is insertion order, which depends
        # on the scrape. The seed only means something over a fixed order.
        ordered = sorted(members, key=lambda m: m[0].ref.venue_id)
        for v, c in rng.sample(ordered, min(quota, len(ordered))):
            row = v.to_row()
            rows.append({
                **{k: row[k] for k in ("venue_id", "name", "category",
                                       "address", "city", "total", "unique",
                                       "monthly", "url")},
                **{f: "" for f in ANSWER_FIELDS},
                "predicted_kind": c.kind.value,
                "predicted_verdict": c.verdict.value,
                "reason": c.reason,
                "_stratum": bucket,
            })

    log.info("Sampled %d venue(s) from %d bucket(s) of %d total",
             len(rows), len(sizes), len(venues))
    return LabelSheet(rows=rows, stratum_sizes=sizes)


def score_labels(
    rows: list[dict[str, Any]], stratum_sizes: dict[str, int]
) -> dict[str, Any]:
    """Weighted error rates, by direction, plus the rows behind them.

    Returns rates rather than one accuracy number on purpose. A private space
    kept is someone's front door on a shared map; a real venue dropped is one
    bar to re-add from a visible list. Averaging them hides the expensive one
    behind the cheap one.
    """
    labelled: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if _answered(row):
            labelled[row["_stratum"]].append(row)

    missing = [s for s in stratum_sizes if not labelled.get(s)]
    if missing:
        raise PartialStratum(
            f"No labels at all for bucket(s): {', '.join(sorted(missing))}. "
            f"Those venues still count toward the population, so the estimate "
            f"would silently exclude them. Label at least a few of each."
        )

    warnings: list[str] = []
    # Inverse sampling probability, recomputed from what was labelled rather
    # than from what was offered -- a row left blank was never observed.
    weight = {s: stratum_sizes[s] / len(labelled[s]) for s in labelled}
    for stratum, got in sorted(labelled.items()):
        if len(got) < THIN_STRATUM:
            warnings.append(
                f"Bucket {stratum!r} has only {len(got)} labelled row(s) "
                f"standing in for {stratum_sizes[stratum]}; its weight of "
                f"{weight[stratum]:.0f}x makes the estimate fragile."
            )

    population = sum(stratum_sizes.values())
    disagreements: dict[str, list[dict]] = defaultdict(list)

    def weighted(predicate, group=None) -> tuple[float, float]:
        """(estimated count matching, estimated size of the group)."""
        hits = size = 0.0
        for stratum, got in labelled.items():
            if group and not stratum.startswith(f"{group}:"):
                continue
            size += stratum_sizes[stratum]
            hits += weight[stratum] * sum(1 for r in got if predicate(r))
        return hits, size

    def rate(hits: float, size: float) -> float | None:
        return hits / size if size else None

    # 1. A private space the classifier kept. The expensive direction.
    def is_private(r):
        keep = _yes(r.get("is_public"))
        if not keep and str(r.get("is_public", "")).strip():
            disagreements["private_space_kept"].append(r)
            return True
        return False

    private_kept, kept_size = weighted(is_private, Verdict.KEEP.value)

    # 2. A real, open venue the classifier dropped. The cheap direction.
    def is_real(r):
        shut = str(r.get("is_open", "")).strip().lower()
        alive = _yes(r.get("is_public")) and shut not in {"n", "no", "false"}
        if alive:
            disagreements["real_venue_dropped"].append(r)
        return alive

    real_dropped, dropped_size = weighted(is_real, Verdict.DROP.value)

    # 3. Kind, over every row where a kind was actually supplied.
    def kind_given(r):
        return bool(str(r.get("true_kind", "")).strip())

    def kind_wrong(r):
        if not kind_given(r):
            return False
        if r.get("true_kind", "").strip() != r.get("predicted_kind", ""):
            disagreements["venue_kind_wrong"].append(r)
            return True
        return False

    kind_answered, _ = weighted(kind_given)
    kind_bad, _ = weighted(kind_wrong)
    kind_correct = None
    if kind_answered:
        kind_correct = 1.0 - (kind_bad / kind_answered)
    else:
        warnings.append(
            "No row carried a true_kind, so venue kind accuracy could not be "
            "estimated. Fill that column to answer the ranking question."
        )

    # Population-level estimate, which is the number worth quoting on its own:
    # how much of this scrape is junk.
    est_private, _ = weighted(
        lambda r: bool(str(r.get("is_public", "")).strip())
        and not _yes(r.get("is_public")))

    return {
        "population": population,
        "labelled": sum(len(v) for v in labelled.values()),
        "estimated": {
            "private_venues": round(est_private, 1),
            "kept": round(kept_size, 1),
            "dropped": round(dropped_size, 1),
        },
        "rates": {
            "private_space_kept": rate(private_kept, kept_size),
            "real_venue_dropped": rate(real_dropped, dropped_size),
            "venue_kind_correct": kind_correct,
        },
        "per_stratum": {
            s: {"population": stratum_sizes[s], "labelled": len(labelled[s]),
                "weight": round(weight[s], 1)}
            for s in sorted(labelled)
        },
        "disagreements": {k: v for k, v in disagreements.items()},
        "warnings": warnings,
    }


__all__ = ["LabelSheet", "PartialStratum", "Kind", "score_labels",
           "stratified_sample", "venues_from_csv", "write_sheet",
           "read_sheet", "SHEET_FIELDS", "ANSWER_FIELDS", "DEFAULT_QUOTA"]


# --- CSV round-trip --------------------------------------------------------
def venues_from_csv(path: Path) -> list[Venue]:
    """Rebuild Venues from any CSV this project writes.

    Only the fields the classifier reads are reconstructed; coordinates and
    geo_source are irrelevant to a labelling pass and are left at their
    defaults rather than being half-restored.
    """
    def number(value: str | None) -> int | None:
        text = (value or "").strip().replace(",", "")
        return int(text) if text.isdigit() else None

    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    out: list[Venue] = []
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        out.append(Venue(
            ref=VenueRef(
                venue_id=(r.get("venue_id") or "").strip() or name,
                slug=(r.get("url") or "").rstrip("/").rpartition("/")[2],
                name=name,
                category=(r.get("category") or "").strip() or None,
                address=(r.get("address") or "").strip() or None,
                city=(r.get("city") or "").strip() or None,
            ),
            total=number(r.get("total")),
            unique=number(r.get("unique")),
            monthly=number(r.get("monthly")),
            you=number(r.get("you")),
        ))
    return out


def write_sheet(sheet: LabelSheet, path: Path) -> Path:
    """Write the labelling sheet, with the bucket sizes in a header comment.

    The sizes have to survive the round-trip or scoring cannot reweight, and
    a sidecar file would get separated from the sheet the moment anyone
    emailed it to whoever is doing the labelling.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        fh.write(f"# {SIZES_HEADER}{json.dumps(sheet.stratum_sizes)}\n")
        writer = csv.DictWriter(fh, fieldnames=list(SHEET_FIELDS))
        writer.writeheader()
        writer.writerows(sheet.rows)
    log.info("Wrote %d row(s) to label -> %s", len(sheet.rows), path)
    return path


def read_sheet(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Read a sheet back, sizes included."""
    text = path.read_text(encoding="utf-8-sig").splitlines()
    if not text or SIZES_HEADER not in text[0]:
        raise ValueError(
            f"{path.name} has no bucket-size header, so the answers cannot be "
            f"reweighted. Re-generate the sheet with `label` and copy the "
            f"answers across rather than editing the header by hand."
        )
    sizes = json.loads(text[0].split(SIZES_HEADER, 1)[1])
    return list(csv.DictReader(text[1:])), sizes
