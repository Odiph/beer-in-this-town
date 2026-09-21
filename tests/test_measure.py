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
    LabelsUnusable,
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


@pytest.mark.unit
def test_a_row_with_no_venue_id_gets_no_url_rather_than_a_broken_one():
    """A CSV predating the url column must not produce a plausible 404.

    `https://untappd.com/v//American Taproom - Waterloo` looks like a link,
    survives into the CSV and the KML's "View on Untappd", and fails only when
    somebody follows it. Absent beats confidently wrong -- the same rule the
    style-line parser and the corpus gate already follow.
    """
    from beer_in_this_town.models import VenueRef

    real = VenueRef(venue_id="7480946", slug="american-taproom-waterloo",
                    name="American Taproom", category=None, address=None, city=None)
    assert real.url == "https://untappd.com/v/american-taproom-waterloo/7480946"

    reconstructed = VenueRef(venue_id="American Taproom", slug="",
                             name="American Taproom", category=None,
                             address=None, city=None)
    assert reconstructed.url == ""


# --- what the review found: rates that did not mean what they claimed ------
def _mixed(n_bar=90, n_cafe=10, n_private=5):
    """A corpus with all three drop reasons, so each is judged on its own claim."""
    return ([_venue(str(i)) for i in range(n_bar)]
            + [_venue(f"c{i}", category="Cafe") for i in range(n_cafe)]
            + [_venue(f"p{i}", total=400, unique=1, monthly=5) for i in range(n_private)])


def _answer(sheet, per_stratum):
    return [dict(r, **per_stratum.get(r["_stratum"], {})) for r in sheet.rows]


@pytest.mark.unit
def test_a_correctly_dropped_cafe_is_not_an_error():
    """Every cafe in drop:non_beer is a real, public, open place.

    Judging a drop by "was this a real venue" therefore scored a perfectly
    correct drop as a mistake: with every label right, the rate read 1.0. A
    drop has to be judged against the claim the classifier actually made.
    """
    sheet = stratified_sample(_mixed(), quota=5, seed=1)
    rows = _answer(sheet, {
        "keep:craft_beer_bar": {"is_public": "y", "is_open": "y",
                                "true_kind": "craft_beer_bar"},
        "drop:non_beer": {"is_public": "y", "is_open": "y", "true_kind": "non_beer"},
        "drop:private": {"is_public": "n", "is_open": "?", "true_kind": ""},
    })
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["rates"]["real_venue_dropped"] == pytest.approx(0.0)
    assert report["rates"]["private_space_kept"] == pytest.approx(0.0)


@pytest.mark.unit
def test_a_beer_venue_dropped_as_non_beer_is_an_error():
    """The other direction of the same bucket must still be caught."""
    sheet = stratified_sample(_mixed(), quota=5, seed=1)
    rows = _answer(sheet, {
        "keep:craft_beer_bar": {"is_public": "y", "is_open": "y",
                                "true_kind": "craft_beer_bar"},
        "drop:non_beer": {"is_public": "y", "is_open": "y", "true_kind": "brewery"},
        "drop:private": {"is_public": "n", "is_open": "?", "true_kind": ""},
    })
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["rates"]["real_venue_dropped"] > 0.5
    assert report["disagreements"]["real_venue_dropped"]


@pytest.mark.unit
def test_a_blank_answer_is_not_scored_as_no_error():
    """A blank used to read as 'not private', so an unlabelled corpus read clean."""
    sheet = stratified_sample(_mixed(n_cafe=0, n_private=5), quota=5, seed=1)
    # Only true_kind is filled on the kept rows: is_public was never answered.
    rows = _answer(sheet, {
        "keep:craft_beer_bar": {"true_kind": "craft_beer_bar"},
        "drop:private": {"is_public": "n", "is_open": "?"},
    })
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["rates"]["private_space_kept"] is None, \
        "no answer to the question means unknown, never 0.0"


@pytest.mark.unit
def test_not_sure_is_an_abstention_not_a_verdict():
    """'?' used to count as private, inflating the expensive rate to 1.0."""
    sheet = stratified_sample(_mixed(n_cafe=0), quota=5, seed=1)
    rows = _answer(sheet, {
        "keep:craft_beer_bar": {"is_public": "?", "is_open": "?"},
        "drop:private": {"is_public": "n", "is_open": "?"},
    })
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["rates"]["private_space_kept"] is None
    assert report["abstained"]["is_public"] >= 5


@pytest.mark.unit
@pytest.mark.parametrize("field,value", [
    ("is_public", "maybe"), ("is_open", "closed"), ("true_kind", "Beer Bar"),
])
def test_an_answer_outside_the_vocabulary_stops_the_score(field, value):
    """'closed' read as not-closed, silently. Name the row instead of guessing."""
    sheet = stratified_sample(_mixed(n_cafe=0), quota=5, seed=1)
    rows = _answer(sheet, {
        "keep:craft_beer_bar": {"is_public": "y", "is_open": "y",
                                "true_kind": "craft_beer_bar"},
        "drop:private": {"is_public": "n", "is_open": "?"},
    })
    rows[0][field] = value
    with pytest.raises(LabelsUnusable, match=field):
        score_labels(rows, sheet.stratum_sizes)


@pytest.mark.unit
def test_common_spellings_are_accepted():
    """Rejecting 'yes' would be pedantry, not rigour."""
    sheet = stratified_sample(_mixed(n_cafe=0), quota=5, seed=1)
    rows = _answer(sheet, {
        "keep:craft_beer_bar": {"is_public": "Yes", "is_open": "YES",
                                "true_kind": "Craft Beer Bar"},
        "drop:private": {"is_public": "no", "is_open": "?"},
    })
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["rates"]["private_space_kept"] == pytest.approx(0.0)
    assert report["rates"]["venue_kind_correct"] == pytest.approx(1.0)


@pytest.mark.unit
def test_an_abstaining_prediction_is_not_scored_as_a_wrong_kind():
    """`unsettled` is a deliberate abstention in classify.py.

    Scoring it wrong turned kind accuracy into a measure of how often the
    category line was blank -- 0.0 on a CSV with no category column, which
    AGENTS.md describes as saying nothing.
    """
    sheet = stratified_sample([_venue(str(i), category=None) for i in range(40)],
                              quota=5, seed=1)
    rows = _answer(sheet, {"review:unsettled": {"is_public": "y", "is_open": "y",
                                                "true_kind": "craft_beer_bar"}})
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["rates"]["venue_kind_correct"] is None
    assert any("kind" in w for w in report["warnings"])


@pytest.mark.unit
def test_a_row_from_a_different_sheet_is_refused():
    """A hand-edited or stale sheet used to KeyError into unexpected_error."""
    sheet = stratified_sample(_mixed(n_cafe=0), quota=5, seed=1)
    rows = _answer(sheet, {
        "keep:craft_beer_bar": {"is_public": "y", "is_open": "y"},
        "drop:private": {"is_public": "n", "is_open": "?"},
    })
    rows[0]["_stratum"] = "keep:invented_bucket"
    with pytest.raises(LabelsUnusable, match="invented_bucket"):
        score_labels(rows, sheet.stratum_sizes)


@pytest.mark.unit
def test_a_closed_venue_kept_is_reported_on_its_own():
    """#7's direction: the classifier kept something that has shut."""
    sheet = stratified_sample(_mixed(n_cafe=0), quota=5, seed=1)
    rows = _answer(sheet, {
        "keep:craft_beer_bar": {"is_public": "y", "is_open": "n",
                                "true_kind": "craft_beer_bar"},
        "drop:private": {"is_public": "n", "is_open": "?"},
    })
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["rates"]["closed_venue_kept"] == pytest.approx(1.0)
    assert report["rates"]["private_space_kept"] == pytest.approx(0.0)


# --- the sheet has to survive a spreadsheet --------------------------------
@pytest.mark.unit
def test_the_sheet_survives_a_round_trip_through_a_spreadsheet(tmp_path):
    """The labeller opens this in Excel or Sheets, which rewrites the file.

    The bucket sizes used to ride in a `#` comment on line 1. Any spreadsheet
    parses that as CSV -- commas split it into cells, quotes double -- and
    saving puts it back mangled, so `score` failed with a remedy ("re-generate
    and copy your answers across") that fails in exactly the same way.
    """
    import csv as _csv

    from beer_in_this_town.measure import read_sheet, write_sheet

    sheet = stratified_sample(_mixed(n_cafe=4, n_private=3), quota=4, seed=1)
    path = tmp_path / "labels.csv"
    write_sheet(sheet, path)

    # What a spreadsheet does: read every line as CSV, write every cell back.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        grid = list(_csv.reader(fh))
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        _csv.writer(fh).writerows(grid)

    rows, sizes = read_sheet(path)
    assert sizes == sheet.stratum_sizes
    assert len(rows) == len(sheet.rows)
    assert {r["_stratum"] for r in rows} == set(sheet.stratum_sizes)


@pytest.mark.unit
def test_a_sheet_saved_as_ansi_still_reads(tmp_path):
    """Excel's default save is the system codepage, not UTF-8."""
    from beer_in_this_town.measure import read_sheet, write_sheet

    sheet = stratified_sample(_mixed(n_cafe=0, n_private=2), quota=3, seed=1)
    path = tmp_path / "labels.csv"
    write_sheet(sheet, path)
    # Rename whatever row was actually sampled, rather than assuming one.
    original = sheet.rows[0]["name"]
    text = path.read_text(encoding="utf-8-sig").replace(original, "Café Münster")
    path.write_bytes(text.encode("cp1252"))

    rows, sizes = read_sheet(path)
    assert sizes == sheet.stratum_sizes
    assert any("Münster" in r["name"] for r in rows)


@pytest.mark.unit
def test_a_rebuilt_venue_keeps_a_working_untappd_url(tmp_path):
    """The slug is the second-to-last segment, not the last.

    Taking the last one produced https://untappd.com/v/<id>/<id> for every row
    in the labelling sheet -- a dead link, on the button a human clicks to
    judge the venue.
    """
    import csv as _csv

    from beer_in_this_town.measure import venues_from_csv

    path = tmp_path / "v.csv"
    url = "https://untappd.com/v/american-taproom-waterloo/7480946"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=["venue_id", "name", "url"])
        w.writeheader()
        w.writerow({"venue_id": "7480946", "name": "American Taproom", "url": url})
    assert venues_from_csv(path)[0].ref.url == url


@pytest.mark.unit
def test_a_row_with_no_url_still_yields_no_url(tmp_path):
    import csv as _csv

    from beer_in_this_town.measure import venues_from_csv

    path = tmp_path / "v.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=["name"])
        w.writeheader()
        w.writerow({"name": "American Taproom"})
    assert venues_from_csv(path)[0].ref.url == ""


@pytest.mark.unit
def test_the_drop_rate_denominator_is_the_buckets_own_question():
    """"Answered any of the three" looked per-question and was not.

    A drop:private row that answered only `is_open` counted in the
    denominator while contributing nothing to the numerator, so a blank
    `is_public` scored as "dropped correctly" -- the same blank-reads-as-
    no-error bug the rewrite was meant to close, one column across.
    """
    sheet = stratified_sample(_mixed(n_cafe=0, n_private=8), quota=8, seed=1)
    rows = _answer(sheet, {
        "keep:craft_beer_bar": {"is_public": "y", "is_open": "y",
                                "true_kind": "craft_beer_bar"},
        # is_public deliberately blank: the question this bucket turns on.
        "drop:private": {"is_open": "y"},
    })
    report = score_labels(rows, sheet.stratum_sizes)
    assert report["rates"]["real_venue_dropped"] is None, \
        "unanswered must be unknown, never zero"


@pytest.mark.unit
def test_not_sure_on_the_buckets_question_is_not_a_verdict_either():
    sheet = stratified_sample(_mixed(n_cafe=0, n_private=8), quota=8, seed=1)
    rows = _answer(sheet, {
        "keep:craft_beer_bar": {"is_public": "y", "is_open": "y",
                                "true_kind": "craft_beer_bar"},
        "drop:private": {"is_public": "?", "is_open": "y"},
    })
    assert score_labels(rows, sheet.stratum_sizes)["rates"]["real_venue_dropped"] is None


@pytest.mark.unit
def test_partially_answered_drops_weight_only_the_rows_that_answered():
    """The weight must be over rows that answered THAT question."""
    sheet = stratified_sample(_mixed(n_cafe=0, n_private=10), quota=10, seed=1)
    rows = []
    private_seen = 0
    for r in sheet.rows:
        row = dict(r)
        if r["_stratum"] == "drop:private":
            private_seen += 1
            if private_seen <= 5:                       # half answer the question
                row["is_public"] = "y" if private_seen == 1 else "n"
            else:                                       # half answer the other one
                row["is_open"] = "y"
        else:
            row.update({"is_public": "y", "is_open": "y",
                        "true_kind": "craft_beer_bar"})
        rows.append(row)
    report = score_labels(rows, sheet.stratum_sizes)
    # 1 of the 5 that answered is wrongly dropped -> 0.2, not 0.1.
    assert report["rates"]["real_venue_dropped"] == pytest.approx(0.2)


@pytest.mark.unit
def test_unsettled_is_not_accepted_as_a_human_answer():
    """It is the classifier declining to decide, not a kind a venue can be."""
    sheet = stratified_sample(_mixed(n_cafe=0, n_private=3), quota=3, seed=1)
    rows = _answer(sheet, {
        "keep:craft_beer_bar": {"is_public": "y", "is_open": "y",
                                "true_kind": "unsettled"},
        "drop:private": {"is_public": "n"},
    })
    with pytest.raises(LabelsUnusable, match="true_kind"):
        score_labels(rows, sheet.stratum_sizes)
