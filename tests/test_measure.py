"""Stratified sampling and reweighted scoring.

Random sampling is the wrong instrument here. Most venues in a city scrape are
ordinary bars, so a random 100 might contain three private spaces -- nowhere
near enough to estimate the rate that matters most. The harness takes a fixed
quota from every bucket including the rare ones, then reweights by inverse
sampling probability so the reported numbers still describe the whole scrape.

The reweighting is the part that can be silently wrong, so it is the part with
the most tests here.
"""
from __future__ import annotations

import pytest

from beer_in_this_town.measure import (
    LabelSheet,
    PartialStratum,
    score_labels,
    stratified_sample,
)
from beer_in_this_town.models import Venue, VenueRef


def _venue(vid: str, category="Beer Bar", total=500, unique=300, monthly=20):
    return Venue(
        ref=VenueRef(venue_id=vid, slug=f"v{vid}", name=f"Venue {vid}",
                     category=category, address="1 Road", city="London"),
        total=total, unique=unique, monthly=monthly, you=None,
    )


def _population(n_public: int, n_private: int) -> list[Venue]:
    """A realistic shape: overwhelmingly ordinary venues, a rare junk tail."""
    public = [_venue(str(i)) for i in range(n_public)]
    private = [_venue(f"p{i}", total=400, unique=1, monthly=5)
               for i in range(n_private)]
    return public + private


# --- sampling --------------------------------------------------------------
@pytest.mark.unit
def test_a_rare_bucket_is_sampled_as_heavily_as_a_common_one():
    """The whole reason not to sample at random."""
    sheet = stratified_sample(_population(990, 10), quota=10, seed=1)
    by_stratum = sheet.counts_by_stratum()
    assert by_stratum["keep:craft_beer_bar"] == 10
    assert by_stratum["drop:private"] == 10


@pytest.mark.unit
def test_a_bucket_smaller_than_the_quota_is_taken_whole():
    sheet = stratified_sample(_population(990, 4), quota=10, seed=1)
    assert sheet.counts_by_stratum()["drop:private"] == 4
    assert sheet.stratum_sizes["drop:private"] == 4


@pytest.mark.unit
def test_sampling_is_reproducible():
    """A labelling sheet someone half-filled must regenerate identically."""
    ids = [
        [r["venue_id"] for r in stratified_sample(
            _population(200, 20), quota=5, seed=7).rows]
        for _ in range(2)
    ]
    assert ids[0] == ids[1]


@pytest.mark.unit
def test_the_sheet_records_what_scoring_needs_to_reweight():
    """Stratum sizes live in the sheet, not in the scorer's memory."""
    sheet = stratified_sample(_population(990, 10), quota=10, seed=1)
    assert sheet.stratum_sizes["keep:craft_beer_bar"] == 990
    assert all(r["_stratum"] for r in sheet.rows)


# --- reweighting -----------------------------------------------------------
def _label(sheet: LabelSheet, **answers) -> list[dict]:
    """Label every sampled row according to what its stratum truly is."""
    out = []
    for row in sheet.rows:
        r = dict(row)
        r.update(answers.get(row["_stratum"], {}))
        out.append(r)
    return out


@pytest.mark.unit
def test_oversampling_a_rare_bucket_does_not_inflate_its_reported_rate():
    """The core claim. 10 private venues in 1000 is 1%, not the 50% of the sample.

    Without reweighting this harness would report a catastrophe on a corpus
    that is 99% fine, and every threshold tuned against it would be wrong.
    """
    population = _population(990, 10)
    sheet = stratified_sample(population, quota=10, seed=1)
    rows = _label(
        sheet,
        **{"keep:craft_beer_bar": {"is_public": "y", "is_open": "y",
                                   "true_kind": "craft_beer_bar"},
           "drop:private": {"is_public": "n", "is_open": "?", "true_kind": ""}},
    )
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["population"] == 1000
    assert report["estimated"]["private_venues"] == pytest.approx(10, abs=0.5)


@pytest.mark.unit
def test_a_private_space_that_was_kept_is_reported_against_the_kept_group():
    """The costly direction: someone's front door survives onto the map."""
    population = _population(990, 10)
    sheet = stratified_sample(population, quota=10, seed=1)
    # Every venue the classifier KEPT turns out to be private. Absurd, but it
    # must land at 100% of the kept group rather than being diluted.
    rows = _label(
        sheet,
        **{"keep:craft_beer_bar": {"is_public": "n", "is_open": "y",
                                   "true_kind": ""},
           "drop:private": {"is_public": "n", "is_open": "?", "true_kind": ""}},
    )
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["rates"]["private_space_kept"] == pytest.approx(1.0)


@pytest.mark.unit
def test_a_real_venue_that_was_dropped_is_reported_against_the_dropped_group():
    """The cheap direction, reported separately because it costs less."""
    population = _population(990, 10)
    sheet = stratified_sample(population, quota=10, seed=1)
    rows = _label(
        sheet,
        **{"keep:craft_beer_bar": {"is_public": "y", "is_open": "y",
                                   "true_kind": "craft_beer_bar"},
           "drop:private": {"is_public": "y", "is_open": "y",
                            "true_kind": "craft_beer_bar"}},
    )
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["rates"]["real_venue_dropped"] == pytest.approx(1.0)
    assert report["rates"]["private_space_kept"] == pytest.approx(0.0)


@pytest.mark.unit
def test_the_two_directions_are_never_collapsed_into_one_accuracy_number():
    """They do not cost the same, so they are not averaged."""
    sheet = stratified_sample(_population(990, 10), quota=10, seed=1)
    rows = _label(
        sheet,
        **{"keep:craft_beer_bar": {"is_public": "y", "is_open": "y",
                                   "true_kind": "craft_beer_bar"},
           "drop:private": {"is_public": "n", "is_open": "?", "true_kind": ""}},
    )
    report = score_labels(rows, sheet.stratum_sizes)
    assert "accuracy" not in report["rates"]
    assert {"private_space_kept", "real_venue_dropped",
            "venue_kind_correct"} <= set(report["rates"])


# --- guarding the weighting ------------------------------------------------
@pytest.mark.unit
def test_a_stratum_nobody_labelled_stops_the_score():
    """Its size still counts in the population; with no sample it cannot be
    estimated, and quietly dropping it would understate the very bucket most
    likely to have been skipped as tedious."""
    sheet = stratified_sample(_population(990, 10), quota=10, seed=1)
    rows = _label(sheet, **{"keep:craft_beer_bar": {"is_public": "y",
                                                    "is_open": "y",
                                                    "true_kind": "craft_beer_bar"}})
    with pytest.raises(PartialStratum, match="drop:private"):
        score_labels(rows, sheet.stratum_sizes)


@pytest.mark.unit
def test_partial_labelling_is_reported_rather_than_assumed_away():
    """Hand-picking rows breaks the weighting, so say so loudly."""
    sheet = stratified_sample(_population(990, 40), quota=20, seed=1)
    rows = []
    for i, row in enumerate(sheet.rows):
        r = dict(row)
        # Label all of the common stratum but only a couple of the rare one.
        if row["_stratum"] == "keep:craft_beer_bar" or i % 9 == 0:
            r.update({"is_public": "y", "is_open": "y",
                      "true_kind": "craft_beer_bar"})
        rows.append(r)
    report = score_labels(rows, sheet.stratum_sizes)
    assert any("drop:private" in w for w in report["warnings"]), report["warnings"]


@pytest.mark.unit
def test_disagreements_come_back_grouped_so_they_can_be_read():
    """A rate tells you how bad; only the rows tell you why."""
    sheet = stratified_sample(_population(990, 10), quota=10, seed=1)
    rows = _label(
        sheet,
        **{"keep:craft_beer_bar": {"is_public": "y", "is_open": "y",
                                   "true_kind": "brewery"},
           "drop:private": {"is_public": "y", "is_open": "y",
                            "true_kind": "craft_beer_bar"}},
    )
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["disagreements"]["real_venue_dropped"]
    assert report["disagreements"]["venue_kind_wrong"]
    example = report["disagreements"]["venue_kind_wrong"][0]
    assert example["predicted_kind"] == "craft_beer_bar"
    assert example["true_kind"] == "brewery"


@pytest.mark.unit
def test_an_unlabelled_kind_does_not_count_against_kind_accuracy():
    """Blank means not answered. Scoring it as wrong invents a failure."""
    sheet = stratified_sample(_population(990, 10), quota=10, seed=1)
    rows = _label(
        sheet,
        **{"keep:craft_beer_bar": {"is_public": "y", "is_open": "y",
                                   "true_kind": ""},
           "drop:private": {"is_public": "n", "is_open": "?", "true_kind": ""}},
    )
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["rates"]["venue_kind_correct"] is None
    assert any("kind" in w for w in report["warnings"])


# --- reading real CSVs -----------------------------------------------------
@pytest.mark.unit
def test_a_csv_with_no_category_column_is_read_without_inventing_one():
    """The seed CSV has rank/name/address/total/unique/monthly/you and no more.

    Reading it must not crash and must not conjure a category, or every kind
    prediction becomes a fabrication rather than an abstention.
    """
    import csv as _csv

    from beer_in_this_town.measure import venues_from_csv

    path = pytest.importorskip("pathlib").Path("_seedlike.csv")
    rows = [{"rank": "1", "name": "American Taproom", "address": "261 Waterloo St",
             "total": "20,259", "unique": "2,451", "monthly": "136", "you": "0"}]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    try:
        venues = venues_from_csv(path)
    finally:
        path.unlink()

    assert len(venues) == 1
    assert venues[0].ref.category is None
    assert venues[0].total == 20259, "thousands separators must survive"
