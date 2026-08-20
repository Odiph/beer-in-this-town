"""Offline tests for note formatting and selection."""
from __future__ import annotations

import pytest

from untappd_maps.notes import format_note, notes_from_csv


@pytest.mark.unit
def test_note_carries_rank_and_all_three_stats():
    note = format_note(
        {"rank": "45", "total": "2628", "unique": "807", "monthly": "10"}
    )
    assert note == "Untappd #45 | 2,628 check-ins | 807 unique | 10/month"


@pytest.mark.unit
def test_thousands_separators_are_added():
    note = format_note({"rank": "1", "total": "20259", "unique": "2451",
                        "monthly": "136"})
    assert "20,259 check-ins" in note
    assert "2,451 unique" in note


@pytest.mark.unit
def test_zero_monthly_is_still_reported():
    """0/month is information; a missing value is not."""
    assert "0/month" in format_note({"rank": "80", "total": "882",
                                     "unique": "6", "monthly": "0"})


@pytest.mark.unit
def test_missing_fields_are_omitted_not_guessed():
    note = format_note({"rank": "5", "total": "100", "unique": "",
                        "monthly": "n/a"})
    assert note == "Untappd #5 | 100 check-ins"


@pytest.mark.unit
def test_note_is_stable_across_calls():
    """The pass diffs against this string to avoid pointless rewrites."""
    row = {"rank": "3", "total": "9223", "unique": "783", "monthly": "259"}
    assert format_note(row) == format_note(dict(row))


@pytest.mark.unit
def test_row_with_no_stats_yields_no_note():
    assert format_note({"name": "Somewhere"}) == ""


@pytest.mark.unit
def test_notes_from_csv_skips_rows_without_stats(tmp_path):
    path = tmp_path / "v.csv"
    path.write_text(
        "rank,name,address,total,unique,monthly\n"
        "1,Bar One,1 Foo St,100,10,5\n"
        "2,Bar Two,2 Bar St,,,\n",
        encoding="utf-8",
    )
    places = notes_from_csv(path)
    assert len(places) == 1
    assert places[0][0] == "Bar One"
    assert "100 check-ins" in places[0][2]


# --- data date ------------------------------------------------------------
@pytest.mark.unit
def test_note_carries_the_data_date():
    note = format_note({"rank": "45", "total": "2628", "unique": "807",
                        "monthly": "10"}, "2026-08-20")
    assert note.endswith("as of 2026-08-20")


@pytest.mark.unit
def test_date_comes_from_the_filename_stamp(tmp_path):
    from untappd_maps.notes import data_date

    path = tmp_path / "venues_singapore_2026-08-20.csv"
    path.write_text("x", encoding="utf-8")
    assert data_date(path) == "2026-08-20"


@pytest.mark.unit
def test_date_falls_back_to_mtime_when_unstamped(tmp_path):
    import re

    from untappd_maps.notes import data_date

    path = tmp_path / "custom.csv"
    path.write_text("x", encoding="utf-8")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_date(path))


@pytest.mark.unit
def test_note_is_dated_by_the_data_not_by_today(tmp_path):
    """Regression guard for a pointless-rewrite trap.

    If the note said "as of <today>", every note would differ from the stored
    one every day, so a daily run would rewrite all of them against a
    rate-limited budget while saying nothing new. The date must track the data.
    """
    path = tmp_path / "venues_singapore_2026-01-15.csv"
    path.write_text(
        "rank,name,address,total,unique,monthly\n1,Bar,1 St,100,10,5\n",
        encoding="utf-8",
    )
    (_, _, note) = notes_from_csv(path)[0]
    assert "as of 2026-01-15" in note

    # Same file read again -> byte-identical note, so nothing is rewritten.
    assert notes_from_csv(path)[0][2] == note
