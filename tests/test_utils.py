"""Tests for air_download.utils."""

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from air_download.utils import (
    build_date_ranges,
    build_exam_output_path,
    exam_key,
    parse_datetime,
    write_exams_csv,
)


class TestParseDatetime:
    """Tests for parse_datetime."""

    def test_date_only(self):
        parsed = parse_datetime("2024-01-15")
        assert (parsed.year, parsed.month, parsed.day) == (2024, 1, 15)

    def test_naive_input_gets_an_offset(self):
        # The API's datetime formats all carry an offset.
        assert parse_datetime("2024-01-15T13:30:00").tzinfo is not None

    def test_explicit_offset_is_preserved(self):
        parsed = parse_datetime("2024-01-15T13:30:00-08:00")
        assert parsed.utcoffset() == timedelta(hours=-8)

    def test_trailing_z_is_utc(self):
        assert parse_datetime("2024-01-15T13:30:00Z").utcoffset() == timedelta(0)

    def test_unparseable_raises(self):
        with pytest.raises(ValueError, match="Could not parse date"):
            parse_datetime("last tuesday")


class TestBuildDateRanges:
    """Tests for build_date_ranges."""

    def test_no_dates_yields_one_empty_range(self):
        assert build_date_ranges(None, None) == [
            {"start": "", "end": "", "label": ""}
        ]

    def test_end_only_has_no_lower_bound(self):
        ranges = build_date_ranges(None, "2024-01-15")
        assert len(ranges) == 1
        assert ranges[0]["start"] == ""
        assert ranges[0]["end"].startswith("2024-01-15")

    def test_window_within_chunk_is_one_range(self):
        ranges = build_date_ranges("2024-01-01", "2024-01-05")
        assert len(ranges) == 1
        assert ranges[0]["start"].startswith("2024-01-01")
        assert ranges[0]["end"].startswith("2024-01-05")

    def test_exactly_chunk_days_is_one_range(self):
        assert len(build_date_ranges("2024-01-01", "2024-01-08")) == 1

    def test_one_day_over_splits(self):
        assert len(build_date_ranges("2024-01-01", "2024-01-09")) == 2

    def test_partial_final_chunk(self):
        ranges = build_date_ranges("2024-01-01", "2024-01-29")
        assert len(ranges) == 4
        assert ranges[-1]["end"].startswith("2024-01-29")

    def test_ranges_are_contiguous(self):
        ranges = build_date_ranges("2024-01-01", "2024-03-01")
        for earlier, later in zip(ranges, ranges[1:]):
            assert earlier["end"] == later["start"]

    def test_ranges_cover_the_whole_window(self):
        ranges = build_date_ranges("2024-01-01", "2024-03-01")
        assert ranges[0]["start"].startswith("2024-01-01")
        assert ranges[-1]["end"].startswith("2024-03-01")

    def test_custom_chunk_days(self):
        assert len(build_date_ranges("2024-01-01", "2024-01-11", chunk_days=2)) == 5

    def test_missing_end_defaults_to_now(self):
        now = datetime(2024, 1, 20, 12, 0, tzinfo=timezone.utc)
        ranges = build_date_ranges("2024-01-15", None, now=now)
        assert ranges[-1]["end"].startswith("2024-01-20")

    def test_same_start_and_end_yields_one_range(self):
        ranges = build_date_ranges("2024-01-15", "2024-01-15")
        assert len(ranges) == 1

    def test_end_before_start_raises(self):
        with pytest.raises(ValueError, match="ends before it starts"):
            build_date_ranges("2024-02-01", "2024-01-01")

    def test_zero_chunk_days_raises(self):
        with pytest.raises(ValueError, match="chunk_days must be at least 1"):
            build_date_ranges("2024-01-01", "2024-03-01", chunk_days=0)

    def test_every_range_has_the_payload_keys(self):
        for date_range in build_date_ranges("2024-01-01", "2024-02-01"):
            assert set(date_range) == {"start", "end", "label"}


class TestExamKey:
    """Tests for exam_key."""

    def test_prefers_study_uid(self):
        assert exam_key({"studyUid": "1.2.3", "accessionNumber": "111"}) == "1.2.3"

    def test_falls_back_to_accession_and_datetime(self):
        key = exam_key({"accessionNumber": "111", "dateTime": "2024-01-02"})
        assert key == ("111", "2024-01-02")

    def test_empty_study_uid_falls_back(self):
        key = exam_key({"studyUid": "", "accessionNumber": "111", "dateTime": "x"})
        assert key == ("111", "x")

    def test_distinct_exams_get_distinct_keys(self):
        assert exam_key({"studyUid": "1.2.3"}) != exam_key({"studyUid": "1.2.4"})


class TestBuildExamOutputPath:
    """Tests for build_exam_output_path."""

    def test_none_output_uses_current_dir(self):
        exam = {"accessionNumber": "12345"}
        result = build_exam_output_path(None, exam, 0)
        assert result == Path(".") / "12345.zip"

    def test_directory_path_creates_zip(self, tmp_path):
        exam = {"accessionNumber": "12345"}
        result = build_exam_output_path(tmp_path, exam, 0)
        assert result == tmp_path / "12345.zip"

    def test_directory_path_creates_dir(self, tmp_path):
        new_dir = tmp_path / "output"
        exam = {"accessionNumber": "12345"}
        result = build_exam_output_path(new_dir, exam, 0)
        assert new_dir.exists()
        assert result == new_dir / "12345.zip"

    def test_missing_accession_uses_index(self, tmp_path):
        exam = {}
        result = build_exam_output_path(tmp_path, exam, 2)
        assert result == tmp_path / "exam_3.zip"

    def test_zip_path_not_existing(self, tmp_path):
        zip_path = tmp_path / "my_download.zip"
        exam = {"accessionNumber": "12345"}
        result = build_exam_output_path(zip_path, exam, 0)
        assert result == zip_path

    def test_zip_path_existing_appends_index(self, tmp_path):
        zip_path = tmp_path / "my_download.zip"
        zip_path.touch()
        exam = {"accessionNumber": "12345"}
        result = build_exam_output_path(zip_path, exam, 0)
        assert result == tmp_path / "my_download_1.zip"

    def test_zip_path_existing_multiple_indices(self, tmp_path):
        zip_path = tmp_path / "my_download.zip"
        zip_path.touch()
        exam = {"accessionNumber": "12345"}
        r1 = build_exam_output_path(zip_path, exam, 0)
        r2 = build_exam_output_path(zip_path, exam, 1)
        assert r1 == tmp_path / "my_download_1.zip"
        assert r2 == tmp_path / "my_download_2.zip"


class TestWriteExamsCsv:
    """Tests for write_exams_csv."""

    def test_creates_csv_with_header(self, tmp_path):
        exams = [
            {
                "accessionNumber": "111",
                "dateTime": "2024-01-01",
                "sex": "M",
                "birthdate": "1990-01-01",
                "description": "BRAIN MRI",
                "imageCount": 100,
            }
        ]
        result = write_exams_csv(exams, tmp_path, mrn="MRN001")
        assert result.exists()

        with open(result) as f:
            reader = csv.reader(f)
            rows = list(reader)

        assert rows[0] == [
            "mrn",
            "accession_number",
            "date_time",
            "sex",
            "birthdate",
            "description",
            "image_count",
        ]
        assert rows[1] == [
            "MRN001",
            "111",
            "2024-01-01",
            "M",
            "1990-01-01",
            "BRAIN MRI",
            "100",
        ]

    def test_appends_without_duplicate_header(self, tmp_path):
        exams = [{"accessionNumber": "111", "dateTime": "", "sex": "", "birthdate": "", "description": "", "imageCount": 0}]
        write_exams_csv(exams, tmp_path, mrn="MRN001")
        write_exams_csv(exams, tmp_path, mrn="MRN002")

        with open(tmp_path / "accessions.csv") as f:
            reader = csv.reader(f)
            rows = list(reader)

        # One header + two data rows
        assert len(rows) == 3
        assert rows[0][0] == "mrn"
        assert rows[1][0] == "MRN001"
        assert rows[2][0] == "MRN002"

    def test_handles_commas_in_description(self, tmp_path):
        exams = [
            {
                "accessionNumber": "222",
                "dateTime": "",
                "sex": "",
                "birthdate": "",
                "description": "BRAIN, WITH CONTRAST, AXIAL",
                "imageCount": 50,
            }
        ]
        result = write_exams_csv(exams, tmp_path)

        with open(result) as f:
            reader = csv.reader(f)
            rows = list(reader)

        # csv module should properly quote the description
        assert rows[1][5] == "BRAIN, WITH CONTRAST, AXIAL"
