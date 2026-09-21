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
    # Scoring needs each bucket's true size to reweight. It rides in a
    # column, not a header comment, so a spreadsheet round-trip keeps it.
    "_stratum_size",
)

DEFAULT_QUOTA = 18  # ~7 buckets -> ~125 rows -> ~45 minutes, once.

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
                "_stratum_size": len(members),
            })

    log.info("Sampled %d venue(s) from %d bucket(s) of %d total",
             len(rows), len(sizes), len(venues))
    return LabelSheet(rows=rows, stratum_sizes=sizes)


YES = {"y", "yes", "true"}
NO = {"n", "no", "false"}
UNSURE = {"?", "unsure", "unknown", "idk"}

# Judging a drop against the reason it was dropped for. A blanket "was this a
# real venue" cannot judge a drop: every café in `drop:non_beer` is a real,
# public, open place, so a blanket rule scores a perfectly correct drop as an
# error. The classifier made a specific claim about each bucket; that claim is
# what the label has to contradict.
DROP_IS_WRONG_WHEN = {
    "private": ("is_public", YES),   # dropped as a home, but it is public
    "closed": ("is_open", YES),      # dropped as gone, but it still trades
    "non_beer": ("true_kind", None),  # dropped as not-beer -- handled below
}
BEER_KINDS = {Kind.BREWERY.value, Kind.BOTTLE_SHOP.value, Kind.CRAFT_BEER_BAR.value}


class LabelsUnusable(RuntimeError):
    """An answer is not one of the values this sheet accepts.

    Guessing at it is how a typo becomes a verdict: `"?"` read as "private"
    inflates the expensive rate, `"closed"` read as "not closed" deflates the
    cheap one, and nothing raises. Naming the row and the value is the only
    honest option.
    """


def _token(row: dict[str, Any], field: str, *, where: str) -> str | None:
    """Normalise one answer to 'y' / 'n' / '?' / None (unanswered)."""
    raw = str(row.get(field, "")).strip().lower()
    if not raw:
        return None
    if raw in YES:
        return "y"
    if raw in NO:
        return "n"
    if raw in UNSURE:
        return "?"
    raise LabelsUnusable(
        f"{where}: {field}={row.get(field)!r} is not one of y / n / ? "
        f"(or yes / no). Fix that cell and re-run."
    )


def _kind(row: dict[str, Any], *, where: str) -> str | None:
    raw = str(row.get("true_kind", "")).strip().lower().replace(" ", "_")
    if not raw:
        return None
    allowed = {k.value for k in Kind} - {Kind.UNSETTLED.value}
    if raw not in allowed:
        raise LabelsUnusable(
            f"{where}: true_kind={row.get('true_kind')!r} is not one of "
            f"{', '.join(sorted(allowed))}. Fix that cell and re-run."
        )
    return raw


def _verdict(row: dict[str, Any]) -> dict[str, Any]:
    """Everything scoring needs from one labelled row, decided exactly once.

    Computing this up front rather than inside predicates means a row is
    judged once, whatever it is later aggregated into -- and the disagreement
    lists are built from the same decision as the rates, not from a side
    effect that happens to run the right number of times.
    """
    where = f"row {row.get('venue_id') or row.get('name') or '?'}"
    stratum = row.get("_stratum", "")
    verdict, _, reason = stratum.partition(":")
    public = _token(row, "is_public", where=where)
    open_ = _token(row, "is_open", where=where)
    kind = _kind(row, where=where)

    wrong_keep_private = verdict == "keep" and public == "n"
    wrong_keep_closed = verdict == "keep" and open_ == "n"

    wrong_drop = False
    if verdict == "drop":
        if reason == "non_beer":
            wrong_drop = kind in BEER_KINDS
        elif reason in DROP_IS_WRONG_WHEN:
            field, _ = DROP_IS_WRONG_WHEN[reason]
            wrong_drop = {"is_public": public, "is_open": open_}[field] == "y"
        else:
            # A bucket this scorer does not know how to judge. Scoring every
            # row in it as correct is the confident-wrong-number failure the
            # whole harness exists to refuse.
            raise LabelsUnusable(
                f"{where}: bucket {stratum!r} has a drop reason this version "
                f"cannot judge. Re-generate the sheet with `label`."
            )

    return {
        "row": row, "stratum": stratum, "verdict": verdict, "reason": reason,
        "is_public": public, "is_open": open_, "true_kind": kind,
        "wrong_keep_private": wrong_keep_private,
        "wrong_keep_closed": wrong_keep_closed,
        "wrong_drop": wrong_drop,
        # Which questions this row actually answered, for per-question
        # denominators. A blank is not an answer and "?" is not a verdict.
        "decided_public": public in {"y", "n"},
        "decided_open": open_ in {"y", "n"},
        "decided_kind": kind is not None,
        # Answered at all, "?" included. Someone who looked and was unsure has
        # labelled the row; they just have not settled any question with it.
        # Conflating that with an untouched row would report a bucket nobody
        # skipped as one nobody opened.
        "answered": any(x is not None for x in (public, open_, kind)),
    }


# Which answer decides a drop, per the claim the classifier made about it.
DROP_QUESTION = {
    "private": "decided_public",
    "closed": "decided_open",
    "non_beer": "decided_kind",
}


def _answered_the_buckets_question(v: dict[str, Any]) -> bool:
    return bool(v.get(DROP_QUESTION.get(v["reason"], ""), False))


def score_labels(
    rows: list[dict[str, Any]], stratum_sizes: dict[str, int]
) -> dict[str, Any]:
    """Weighted error rates, by direction, plus the rows behind them.

    Rates rather than one accuracy number, on purpose. A private space kept is
    someone's front door on a shared map; a venue wrongly dropped costs one bar
    to re-add from a visible list. Averaging them hides the expensive error
    behind the cheap one.

    Every rate's denominator is the rows that answered *that* question. A rate
    over all sampled rows counts a blank as "no error" and reports a corpus
    nobody looked at as clean.
    """
    verdicts: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        stratum = row.get("_stratum", "")
        if stratum not in stratum_sizes:
            raise LabelsUnusable(
                f"Row {row.get('venue_id') or row.get('name')!r} is in bucket "
                f"{stratum!r}, which this sheet does not describe. It was "
                f"probably produced by a different `label` run; re-generate it."
            )
        v = _verdict(row)
        if v["answered"]:
            verdicts[stratum].append(v)

    missing = [s for s in stratum_sizes if not verdicts.get(s)]
    if missing:
        raise PartialStratum(
            f"No usable labels for bucket(s): {', '.join(sorted(missing))}. "
            f"Those venues still count toward the population, so the estimate "
            f"would silently exclude them. Label at least a few of each."
        )

    warnings: list[str] = []
    population = sum(stratum_sizes.values())
    disagreements: dict[str, list[dict]] = defaultdict(list)

    def measure(answered, is_error, group=None, collect=None):
        """Weighted (errors, population) over the rows that answered.

        The weight is recomputed per question: if only four rows in a bucket
        of twenty answered `is_open`, those four stand in for the bucket on
        that question, not the twenty that answered something.
        """
        errors = covered = 0.0
        thin = []
        for stratum, got in verdicts.items():
            if group and not stratum.startswith(f"{group}:"):
                continue
            usable = [v for v in got if answered(v)]
            if not usable:
                thin.append((stratum, 0))
                continue
            if len(usable) < THIN_STRATUM:
                thin.append((stratum, len(usable)))
            weight = stratum_sizes[stratum] / len(usable)
            covered += stratum_sizes[stratum]
            for v in usable:
                if is_error(v):
                    errors += weight
                    if collect:
                        disagreements[collect].append(v["row"])
        return errors, covered, thin

    def rate(errors, covered):
        return errors / covered if covered else None

    metrics = {}
    for name, answered, is_error, group in (
        ("private_space_kept", lambda v: v["decided_public"],
         lambda v: v["wrong_keep_private"], Verdict.KEEP.value),
        ("closed_venue_kept", lambda v: v["decided_open"],
         lambda v: v["wrong_keep_closed"], Verdict.KEEP.value),
        # Per-question means THIS bucket's question. `any of the three` looked
        # per-question and was not: a drop:private row that answered only
        # is_open counted in the denominator while contributing nothing to the
        # numerator, so a blank is_public scored as "dropped correctly" -- the
        # same blank-reads-as-no-error bug this rewrite was meant to close,
        # one column across.
        ("real_venue_dropped", _answered_the_buckets_question,
         lambda v: v["wrong_drop"], Verdict.DROP.value),
    ):
        errors, covered, thin = measure(answered, is_error, group, collect=name)
        metrics[name] = rate(errors, covered)
        for stratum, n in thin:
            warnings.append(
                f"{name}: bucket {stratum!r} has {n} usable answer(s) for "
                f"{stratum_sizes[stratum]} venue(s)"
                + ("; it is excluded from this rate." if n == 0
                   else f"; its weight of {stratum_sizes[stratum] / n:.0f}x "
                        f"makes the estimate fragile.")
            )

    # Kind is scored only where the classifier actually decided. `unsettled`
    # is a deliberate abstention, and scoring an abstention as wrong makes the
    # metric a measure of how often the category line was blank.
    def kind_decided(v):
        predicted = v["row"].get("predicted_kind")
        return v["decided_kind"] and predicted != Kind.UNSETTLED.value

    kind_err, kind_cov, _ = measure(
        kind_decided,
        lambda v: v["row"].get("predicted_kind") != v["true_kind"],
        collect="venue_kind_wrong")
    metrics["venue_kind_correct"] = None if not kind_cov else 1.0 - kind_err / kind_cov
    if not kind_cov:
        warnings.append(
            "venue_kind_correct: no row pairs a true_kind with a decided "
            "prediction, so kind accuracy is unknown rather than zero. A CSV "
            "with no category column can never answer this."
        )

    flat = [v for g in verdicts.values() for v in g]
    abstained = {
        "is_public": sum(1 for v in flat if v["is_public"] == "?"),
        "is_open": sum(1 for v in flat if v["is_open"] == "?"),
    }
    est_private, _, _ = measure(lambda v: v["decided_public"],
                                lambda v: v["is_public"] == "n")

    return {
        "population": population,
        "labelled": sum(len(v) for v in verdicts.values()),
        "estimated": {
            "private_venues": round(est_private, 1),
            "kept": sum(n for s, n in stratum_sizes.items() if s.startswith("keep:")),
            "dropped": sum(n for s, n in stratum_sizes.items() if s.startswith("drop:")),
        },
        "rates": metrics,
        "abstained": abstained,
        "per_stratum": {
            s: {"population": stratum_sizes[s], "usable": len(verdicts[s])}
            for s in sorted(verdicts)
        },
        "disagreements": {k: v for k, v in disagreements.items()},
        "warnings": warnings,
    }

# --- CSV round-trip --------------------------------------------------------
def _slug_from_url(url: str | None) -> str:
    """The slug out of https://untappd.com/v/<slug>/<id>."""
    parts = [p for p in (url or "").rstrip("/").split("/") if p]
    return parts[-2] if len(parts) >= 2 and parts[-1].isdigit() else ""


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
                # .../v/<slug>/<id> -- the slug is the second-to-last
                # segment. Taking the last one made every url in the labelling
                # sheet .../v/<id>/<id>: a dead link, on the button a human
                # clicks to judge the venue.
                slug=_slug_from_url(r.get("url")),
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
    """Write the labelling sheet as plain CSV, nothing but rows.

    The bucket sizes scoring needs ride in a `_stratum_size` column rather
    than a header comment. Whoever labels this opens it in Excel or Sheets,
    and a spreadsheet does not preserve a comment line: it parses it as CSV,
    splits it on its own commas, doubles its quotes and writes it back
    mangled. Scoring then failed with a remedy -- re-generate the sheet and
    copy your answers across -- that failed the same way on the next pass.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(SHEET_FIELDS))
        writer.writeheader()
        writer.writerows(sheet.rows)
    log.info("Wrote %d row(s) to label -> %s", len(sheet.rows), path)
    return path


def read_sheet(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Read a sheet back, recovering each bucket's size from its own rows."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        # Excel saves in the system codepage by default, and a venue name with
        # an accent in it is not an exotic case.
        log.warning("%s is not UTF-8; reading it as cp1252.", path.name)
        text = path.read_text(encoding="cp1252")

    rows = list(csv.DictReader(text.splitlines()))
    sizes: dict[str, int] = {}
    for row in rows:
        stratum = (row.get("_stratum") or "").strip()
        raw = (row.get("_stratum_size") or "").strip()
        if not stratum or not raw.isdigit():
            raise ValueError(
                f"{path.name} is missing the _stratum / _stratum_size columns, "
                f"so the answers cannot be reweighted. Re-generate it with "
                f"`label` and copy the answers across."
            )
        size = int(raw)
        if sizes.setdefault(stratum, size) != size:
            raise ValueError(
                f"{path.name} gives bucket {stratum!r} two different sizes. "
                f"Re-generate it with `label` rather than editing that column."
            )
    return rows, sizes
