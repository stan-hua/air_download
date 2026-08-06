"""Tests for matching ultrasound exams to the CTs that followed them.

Every fixture uses synthetic identifiers written to tmp_path; no real
search output is ever read.
"""

import csv

import pytest

from air_download.match import (
    MATCH_CSV_HEADER,
    match_exams,
    read_exams,
    write_matches_csv,
)

HEADER = "mrn,accession_number,date_time,sex,birthdate,description,image_count"


def write_csv(tmp_path, name, *rows, header=HEADER):
    """Write a synthetic search-result CSV and return its path."""
    path = tmp_path / name
    path.write_text("\n".join([header, *rows]) + "\n")
    return path


def exam(mrn, accession, when, description="EXAM"):
    """Build a parsed exam the way read_exams would."""
    from air_download.utils import parse_datetime

    return {
        "mrn": mrn,
        "accession_number": accession,
        "date_time": when,
        "description": description,
        "when": parse_datetime(when),
    }


class TestReadExams:
    """Tests for read_exams."""

    def test_reads_and_parses(self, tmp_path):
        path = write_csv(
            tmp_path, "us.csv", "A1,111,2021-03-02T08:00:00-08:00,,,US ED BEDSIDE,10"
        )
        exams = read_exams(path)
        assert len(exams) == 1
        assert exams[0]["mrn"] == "A1"
        assert exams[0]["when"].hour == 8

    def test_accepts_date_only(self, tmp_path):
        path = write_csv(tmp_path, "us.csv", "A1,111,2021-03-02,,,US,10")
        assert len(read_exams(path)) == 1

    def test_skips_rows_without_mrn(self, tmp_path, caplog):
        path = write_csv(
            tmp_path,
            "us.csv",
            ",111,2021-03-02T08:00:00-08:00,,,US,10",
            "A2,222,2021-03-02T09:00:00-08:00,,,US,10",
        )
        with caplog.at_level("WARNING"):
            exams = read_exams(path)
        assert [e["mrn"] for e in exams] == ["A2"]
        assert "without an mrn" in caplog.text.lower()

    def test_skips_unparseable_dates(self, tmp_path, caplog):
        path = write_csv(
            tmp_path,
            "us.csv",
            "A1,111,not-a-date,,,US,10",
            "A2,222,2021-03-02T09:00:00-08:00,,,US,10",
        )
        with caplog.at_level("WARNING"):
            exams = read_exams(path)
        assert [e["mrn"] for e in exams] == ["A2"]
        assert "unreadable date_time" in caplog.text.lower()

    def test_missing_column_raises(self, tmp_path):
        path = write_csv(tmp_path, "us.csv", "A1,111", header="mrn,accession_number")
        with pytest.raises(ValueError, match="missing required column"):
            read_exams(path)


class TestMatchExams:
    """Tests for the pairing rules."""

    def test_ct_after_us_within_window_matches(self):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [exam("A1", "C1", "2021-03-02T14:00:00-08:00")]
        matches = match_exams(us, ct)
        assert len(matches) == 1
        assert matches[0]["hours_between"] == 6.0

    def test_ct_before_us_does_not_match(self):
        us = [exam("A1", "U1", "2021-03-02T14:00:00-08:00")]
        ct = [exam("A1", "C1", "2021-03-02T08:00:00-08:00")]
        assert match_exams(us, ct) == []

    def test_simultaneous_exams_do_not_match(self):
        # Neither followed the other.
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [exam("A1", "C1", "2021-03-02T08:00:00-08:00")]
        assert match_exams(us, ct) == []

    def test_exactly_at_window_edge_matches(self):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [exam("A1", "C1", "2021-03-03T08:00:00-08:00")]
        assert len(match_exams(us, ct)) == 1

    def test_just_past_window_does_not_match(self):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [exam("A1", "C1", "2021-03-03T08:00:01-08:00")]
        assert match_exams(us, ct) == []

    def test_different_patients_do_not_match(self):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [exam("A2", "C1", "2021-03-02T14:00:00-08:00")]
        assert match_exams(us, ct) == []

    def test_same_accession_different_patients_kept_apart(self):
        # Accession numbers repeat across patients; MRN decides the pairing.
        us = [
            exam("A1", "111", "2021-03-02T08:00:00-08:00"),
            exam("A2", "111", "2021-06-02T08:00:00-08:00"),
        ]
        ct = [
            exam("A1", "999", "2021-03-02T10:00:00-08:00"),
            exam("A2", "999", "2021-06-02T10:00:00-08:00"),
        ]
        matches = match_exams(us, ct)
        assert {(m["mrn"], m["ct_accession_number"]) for m in matches} == {
            ("A1", "999"),
            ("A2", "999"),
        }

    def test_earliest_qualifying_ct_chosen_by_default(self):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [
            exam("A1", "C_LATE", "2021-03-02T20:00:00-08:00"),
            exam("A1", "C_EARLY", "2021-03-02T10:00:00-08:00"),
        ]
        matches = match_exams(us, ct)
        assert len(matches) == 1
        assert matches[0]["ct_accession_number"] == "C_EARLY"

    def test_all_pairs_emits_every_qualifying_ct(self):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [
            exam("A1", "C1", "2021-03-02T10:00:00-08:00"),
            exam("A1", "C2", "2021-03-02T20:00:00-08:00"),
        ]
        matches = match_exams(us, ct, all_pairs=True)
        assert [m["ct_accession_number"] for m in matches] == ["C1", "C2"]

    def test_multiple_ultrasounds_each_get_a_row(self):
        us = [
            exam("A1", "U1", "2021-03-02T08:00:00-08:00"),
            exam("A1", "U2", "2021-03-02T09:00:00-08:00"),
        ]
        ct = [exam("A1", "C1", "2021-03-02T10:00:00-08:00")]
        matches = match_exams(us, ct)
        assert [m["us_accession_number"] for m in matches] == ["U1", "U2"]

    def test_custom_window(self):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [exam("A1", "C1", "2021-03-02T14:00:00-08:00")]
        assert match_exams(us, ct, max_hours=4) == []
        assert len(match_exams(us, ct, max_hours=8)) == 1

    def test_non_positive_window_raises(self):
        with pytest.raises(ValueError, match="max_hours must be positive"):
            match_exams([], [], max_hours=0)

    def test_timezone_offsets_compared_correctly(self):
        # 08:00-08:00 is 16:00 UTC; 17:00 UTC is one hour later.
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [exam("A1", "C1", "2021-03-02T17:00:00+00:00")]
        matches = match_exams(us, ct)
        assert matches[0]["hours_between"] == 1.0

    def test_crossing_midnight(self):
        us = [exam("A1", "U1", "2021-03-02T23:00:00-08:00")]
        ct = [exam("A1", "C1", "2021-03-03T02:00:00-08:00")]
        assert match_exams(us, ct)[0]["hours_between"] == 3.0

    def test_output_ordered_by_patient_then_time(self):
        us = [
            exam("A2", "U2", "2021-03-02T08:00:00-08:00"),
            exam("A1", "U1b", "2021-03-02T09:00:00-08:00"),
            exam("A1", "U1a", "2021-03-02T07:00:00-08:00"),
        ]
        ct = [
            exam("A1", "C1", "2021-03-02T10:00:00-08:00"),
            exam("A2", "C2", "2021-03-02T10:00:00-08:00"),
        ]
        matches = match_exams(us, ct)
        assert [(m["mrn"], m["us_accession_number"]) for m in matches] == [
            ("A1", "U1a"),
            ("A1", "U1b"),
            ("A2", "U2"),
        ]

    def test_empty_inputs(self):
        assert match_exams([], []) == []
        assert match_exams([exam("A1", "U1", "2021-03-02")], []) == []


class TestWriteMatchesCsv:
    """Tests for the output file."""

    def test_writes_header_and_rows(self, tmp_path):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00", "US ED BEDSIDE")]
        ct = [exam("A1", "C1", "2021-03-02T14:00:00-08:00", "CT ABDOMEN PELVIS")]
        out = write_matches_csv(match_exams(us, ct), tmp_path / "matched.csv")
        with open(out, newline="") as f:
            rows = list(csv.DictReader(f))
        assert list(rows[0]) == MATCH_CSV_HEADER
        assert rows[0]["us_description"] == "US ED BEDSIDE"
        assert rows[0]["ct_description"] == "CT ABDOMEN PELVIS"

    def test_overwrites_rather_than_appends(self, tmp_path):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [exam("A1", "C1", "2021-03-02T14:00:00-08:00")]
        matches = match_exams(us, ct)
        out = tmp_path / "matched.csv"
        write_matches_csv(matches, out)
        write_matches_csv(matches, out)
        with open(out, newline="") as f:
            assert len(list(csv.DictReader(f))) == 1

    def test_empty_matches_still_writes_header(self, tmp_path):
        out = write_matches_csv([], tmp_path / "matched.csv")
        assert out.read_text().strip() == ",".join(MATCH_CSV_HEADER)


class TestEndToEnd:
    """Reading two CSVs through to matched output."""

    def test_full_flow(self, tmp_path):
        us_csv = write_csv(
            tmp_path,
            "us.csv",
            "A1,U1,2021-03-02T08:00:00-08:00,,,US ED BEDSIDE,10",
            "A2,U2,2021-03-05T08:00:00-08:00,,,US ED BEDSIDE,10",
            "A3,U3,2021-03-09T08:00:00-08:00,,,US ED BEDSIDE,10",
        )
        ct_csv = write_csv(
            tmp_path,
            "ct.csv",
            # A1: 6h after -> matches
            "A1,C1,2021-03-02T14:00:00-08:00,,,CT ABDOMEN PELVIS,480",
            # A2: 3 days after -> outside the window
            "A2,C2,2021-03-08T08:00:00-08:00,,,CT ABDOMEN PELVIS,480",
            # A3: before the ultrasound -> excluded
            "A3,C3,2021-03-09T06:00:00-08:00,,,CT ABDOMEN PELVIS,480",
        )
        matches = match_exams(read_exams(us_csv), read_exams(ct_csv))
        assert [m["mrn"] for m in matches] == ["A1"]
