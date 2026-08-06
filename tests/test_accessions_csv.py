"""Tests for reading (MRN, accession) pairs from a search-results CSV.

All fixtures use synthetic identifiers written to tmp_path; no real
search output is ever read.
"""

import pytest

from air_download.utils import read_accession_pairs

HEADER = "mrn,accession_number,date_time,sex,birthdate,description,image_count"


def write_csv(tmp_path, *rows, header=HEADER, name="accessions.csv"):
    """Write a synthetic accessions CSV and return its path."""
    path = tmp_path / name
    path.write_text("\n".join([header, *rows]) + "\n")
    return path


class TestReadAccessionPairs:
    """Tests for read_accession_pairs."""

    def test_reads_mrn_and_accession(self, tmp_path):
        path = write_csv(
            tmp_path,
            "A1,111,2021-03-02,,,CT ABDOMEN PELVIS,480",
            "A2,222,2022-06-11,,,CT ABDOMEN PELVIS,300",
        )
        assert read_accession_pairs(path) == [("A1", "111"), ("A2", "222")]

    def test_same_accession_different_patients_both_kept(self, tmp_path):
        # The reason both columns are required: accession numbers repeat.
        path = write_csv(
            tmp_path,
            "A1,111,2021-03-02,,,CT ABDOMEN PELVIS,480",
            "A2,111,2022-06-11,,,CT ABDOMEN PELVIS,300",
        )
        assert read_accession_pairs(path) == [("A1", "111"), ("A2", "111")]

    def test_exact_duplicates_collapsed(self, tmp_path):
        path = write_csv(
            tmp_path,
            "A1,111,2021-03-02,,,CT ABDOMEN PELVIS,480",
            "A1,111,2021-03-02,,,CT ABDOMEN PELVIS,480",
        )
        assert read_accession_pairs(path) == [("A1", "111")]

    def test_file_order_preserved(self, tmp_path):
        path = write_csv(
            tmp_path,
            "A3,333,2021-01-01,,,CT,10",
            "A1,111,2021-01-02,,,CT,10",
            "A2,222,2021-01-03,,,CT,10",
        )
        assert [mrn for mrn, _ in read_accession_pairs(path)] == ["A3", "A1", "A2"]

    def test_quoted_description_does_not_shift_columns(self, tmp_path):
        path = write_csv(
            tmp_path,
            'A1,111,2021-03-02,,,"CT ABDOMEN PELVIS, AXIAL",480',
        )
        assert read_accession_pairs(path) == [("A1", "111")]

    def test_whitespace_stripped(self, tmp_path):
        path = write_csv(tmp_path, "  A1 ,  111 ,2021-03-02,,,CT,10")
        assert read_accession_pairs(path) == [("A1", "111")]

    def test_rows_missing_mrn_skipped(self, tmp_path, caplog):
        path = write_csv(
            tmp_path,
            ",111,2021-03-02,,,CT,10",
            "A2,222,2022-06-11,,,CT,10",
        )
        with caplog.at_level("WARNING"):
            pairs = read_accession_pairs(path)
        assert pairs == [("A2", "222")]
        assert "skipped 1 row" in caplog.text.lower()

    def test_rows_missing_accession_skipped(self, tmp_path):
        path = write_csv(
            tmp_path,
            "A1,,2021-03-02,,,CT,10",
            "A2,222,2022-06-11,,,CT,10",
        )
        assert read_accession_pairs(path) == [("A2", "222")]

    def test_empty_file_returns_empty(self, tmp_path):
        assert read_accession_pairs(write_csv(tmp_path)) == []

    def test_missing_mrn_column_raises(self, tmp_path):
        path = write_csv(
            tmp_path, "111,CT", header="accession_number,description"
        )
        with pytest.raises(ValueError, match="missing required column"):
            read_accession_pairs(path)

    def test_missing_accession_column_raises(self, tmp_path):
        path = write_csv(tmp_path, "A1,CT", header="mrn,description")
        with pytest.raises(ValueError, match="missing required column"):
            read_accession_pairs(path)

    def test_error_names_the_columns_found(self, tmp_path):
        path = write_csv(tmp_path, "A1,CT", header="patient,study")
        with pytest.raises(ValueError, match="patient, study"):
            read_accession_pairs(path)

    def test_extra_columns_ignored(self, tmp_path):
        path = write_csv(
            tmp_path,
            "A1,111,extra-value",
            header="mrn,accession_number,site",
        )
        assert read_accession_pairs(path) == [("A1", "111")]

    def test_column_order_does_not_matter(self, tmp_path):
        path = write_csv(
            tmp_path, "111,A1", header="accession_number,mrn"
        )
        assert read_accession_pairs(path) == [("A1", "111")]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_accession_pairs(tmp_path / "nope.csv")


class TestRoundTrip:
    """The CSV written by --search-only must be readable back."""

    def test_written_csv_is_readable(self, tmp_path):
        from air_download.utils import write_exams_csv

        exams = [
            {
                "accessionNumber": "111",
                "patientId": "A1",
                "dateTime": "2021-03-02",
                "description": "CT ABDOMEN PELVIS, AXIAL",
                "imageCount": 480,
            },
            {
                "accessionNumber": "222",
                "patientId": "A2",
                "dateTime": "2022-06-11",
                "description": "CT ABDOMEN PELVIS",
                "imageCount": 300,
            },
        ]
        write_exams_csv(exams, tmp_path)
        assert read_accession_pairs(tmp_path / "accessions.csv") == [
            ("A1", "111"),
            ("A2", "222"),
        ]

    def test_appended_csv_deduplicates_on_read(self, tmp_path):
        from air_download.utils import write_exams_csv

        exams = [
            {
                "accessionNumber": "111",
                "patientId": "A1",
                "dateTime": "2021-03-02",
                "description": "CT",
                "imageCount": 480,
            }
        ]
        # --search-only appends, so re-running a search duplicates rows.
        write_exams_csv(exams, tmp_path)
        write_exams_csv(exams, tmp_path)
        assert read_accession_pairs(tmp_path / "accessions.csv") == [("A1", "111")]
