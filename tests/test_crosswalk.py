"""Tests for the pseudonym crosswalk.

Every identifier here is synthetic. The point of most of these tests is that a
second run reuses what the first one assigned, since that is what keeps a
resumed download from filing the same patient twice.
"""

# Standard libraries
import csv
from pathlib import Path

# Non-standard libraries
import pytest

# Custom libraries
from air_download.crosswalk import (
    CROSSWALK_CSV_HEADER,
    Crosswalk,
    default_crosswalk_path,
    parse_anon_ids,
)


def read_rows(path: Path) -> list[dict]:
    """Read a written CSV back."""
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def record(crosswalk: Crosswalk, mrn: str, accession: str, **kwargs) -> str:
    """Assign identifiers for one exam and record it, returning its path."""
    anon_mrn = crosswalk.patient_id(mrn)
    anon_accession = crosswalk.exam_id(mrn, accession)
    visit = kwargs.pop("visit_folder", "visit-01")
    exam_type = kwargs.pop("exam_type", "us")
    archive = f"{anon_mrn}/{visit}/{exam_type}/{anon_accession}.zip"
    crosswalk.record(
        mrn=mrn,
        accession=accession,
        exam_type=exam_type,
        date_time=kwargs.pop("date_time", "2021-03-02T06:00:00-08:00"),
        visit_folder=visit,
        archive_path=archive,
        **kwargs,
    )
    return archive


class TestAssignsIds:
    """Identifiers are sequential, stable, and unique across the cohort."""

    def test_the_first_patient_is_p0001(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        assert crosswalk.patient_id("MRN-A") == "P0001"

    def test_the_same_mrn_returns_the_same_id(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        assert crosswalk.patient_id("MRN-A") == crosswalk.patient_id("MRN-A")

    def test_ids_are_assigned_in_first_seen_order(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        assert crosswalk.patient_id("MRN-A") == "P0001"
        assert crosswalk.patient_id("MRN-B") == "P0002"
        assert crosswalk.patient_id("MRN-A") == "P0001"

    def test_the_first_exam_is_a0001(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        assert crosswalk.exam_id("MRN-A", "ACC-1") == "A0001"

    def test_the_same_accession_under_two_patients_gets_two_ids(self, tmp_path):
        # An accession number is only unique within a patient, so keying on it
        # alone would file two patients' exams as one.
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        first = crosswalk.exam_id("MRN-A", "SHARED")
        second = crosswalk.exam_id("MRN-B", "SHARED")
        assert first != second

    def test_exam_ids_are_unique_across_patients(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        assigned = [
            crosswalk.exam_id("MRN-A", "ACC-1"),
            crosswalk.exam_id("MRN-A", "ACC-2"),
            crosswalk.exam_id("MRN-B", "ACC-1"),
        ]
        assert assigned == ["A0001", "A0002", "A0003"]

    def test_counts_are_reported(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        record(crosswalk, "MRN-A", "ACC-1")
        record(crosswalk, "MRN-B", "ACC-2")
        assert crosswalk.n_patients == 2
        assert len(crosswalk) == 2


class TestResume:
    """A second run must continue the first, not start over beside it."""

    def test_a_row_is_on_disk_before_anything_is_flushed(self, tmp_path):
        # A run that dies at 3 GB of 4 GB must still be re-identifiable.
        path = tmp_path / "cw.csv"
        record(Crosswalk(path), "MRN-A", "ACC-1")
        assert len(read_rows(path)) == 1

    def test_reloading_reuses_an_existing_id(self, tmp_path):
        path = tmp_path / "cw.csv"
        record(Crosswalk(path), "MRN-A", "ACC-1")
        resumed = Crosswalk(path)
        assert resumed.patient_id("MRN-A") == "P0001"
        assert resumed.exam_id("MRN-A", "ACC-1") == "A0001"

    def test_a_new_patient_added_later_continues_the_numbering(self, tmp_path):
        path = tmp_path / "cw.csv"
        record(Crosswalk(path), "MRN-A", "ACC-1")
        resumed = Crosswalk(path)
        assert resumed.patient_id("MRN-B") == "P0002"
        assert resumed.exam_id("MRN-B", "ACC-2") == "A0002"

    def test_the_counter_resumes_from_the_highest_id_not_the_row_count(
        self, tmp_path
    ):
        # A hand-edited or truncated crosswalk must not reissue an id that a
        # folder on disk is already using.
        path = tmp_path / "cw.csv"
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CROSSWALK_CSV_HEADER)
            writer.writeheader()
            writer.writerow(
                {
                    "anon_mrn": "P0009",
                    "anon_accession_number": "A0042",
                    "visit_folder": "visit-03",
                    "exam_type": "us",
                    "archive_path": "P0009/visit-03/us/A0042.zip",
                    "mrn": "MRN-A",
                    "accession_number": "ACC-1",
                    "date_time": "2021-03-02T06:00:00-08:00",
                }
            )
        resumed = Crosswalk(path)
        assert resumed.patient_id("MRN-B") == "P0010"
        assert resumed.exam_id("MRN-B", "ACC-2") == "A0043"

    def test_recording_the_same_archive_twice_writes_one_row(self, tmp_path):
        path = tmp_path / "cw.csv"
        crosswalk = Crosswalk(path)
        record(crosswalk, "MRN-A", "ACC-1")
        record(crosswalk, "MRN-A", "ACC-1")
        assert len(read_rows(path)) == 1

    def test_a_resumed_run_appends_nothing_for_what_it_skips(self, tmp_path):
        path = tmp_path / "cw.csv"
        record(Crosswalk(path), "MRN-A", "ACC-1")
        record(Crosswalk(path), "MRN-A", "ACC-1")
        assert len(read_rows(path)) == 1

    def test_a_file_missing_a_column_raises_naming_what_was_found(self, tmp_path):
        path = tmp_path / "cw.csv"
        path.write_text("anon_mrn,mrn\nP0001,MRN-A\n")
        with pytest.raises(ValueError) as exc:
            Crosswalk(path)
        assert "anon_accession_number" in str(exc.value)
        assert "anon_mrn" in str(exc.value)

    def test_read_only_records_nothing_on_disk(self, tmp_path):
        path = tmp_path / "cw.csv"
        crosswalk = Crosswalk(path, read_only=True)
        record(crosswalk, "MRN-A", "ACC-1")
        assert not path.exists()

    def test_read_only_still_loads_an_existing_file(self, tmp_path):
        # So a dry run over a started cohort previews the real paths.
        path = tmp_path / "cw.csv"
        record(Crosswalk(path), "MRN-A", "ACC-1")
        preview = Crosswalk(path, read_only=True)
        assert preview.patient_id("MRN-A") == "P0001"


class TestColumnOrder:
    """The anonymous columns come first, so a prefix of the file is shareable."""

    def test_the_real_columns_come_after_the_anon_ones(self, tmp_path):
        path = tmp_path / "cw.csv"
        record(Crosswalk(path), "MRN-A", "ACC-1")
        (row,) = read_rows(path)
        safe = CROSSWALK_CSV_HEADER[:5]
        assert "mrn" not in safe and "accession_number" not in safe
        assert "date_time" not in safe
        assert [row[c] for c in safe] == [
            "P0001",
            "A0001",
            "visit-01",
            "us",
            "P0001/visit-01/us/A0001.zip",
        ]


class TestVisitOrdinals:
    """Visits are ordered within a patient, and never renumbered."""

    def test_the_first_visit_is_visit_01(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        assert crosswalk.visit_folder("MRN-A", "U1", "C1") == "visit-01"

    def test_the_same_pair_twice_keeps_one_folder(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        first = crosswalk.visit_folder("MRN-A", "U1", "C1")
        assert crosswalk.visit_folder("MRN-A", "U1", "C1") == first

    def test_two_pairs_for_one_patient_get_distinct_ordinals(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        assert crosswalk.visit_folder("MRN-A", "U1", "C1") == "visit-01"
        assert crosswalk.visit_folder("MRN-A", "U2", "C2") == "visit-02"

    def test_two_pairs_on_the_same_day_do_not_collide(self, tmp_path):
        # The old layout named the folder for the date and had to suffix this
        # case; an ordinal makes the collision impossible.
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        when = "2021-03-02T06:00:00-08:00"
        first = crosswalk.visit_folder("MRN-A", "U1", "C1", when)
        second = crosswalk.visit_folder("MRN-A", "U2", "C2", when)
        assert first != second

    def test_ordinals_are_per_patient(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        assert crosswalk.visit_folder("MRN-A", "U1", "C1") == "visit-01"
        assert crosswalk.visit_folder("MRN-B", "U2", "C2") == "visit-01"

    def test_ordinals_follow_the_order_they_are_assigned_in(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        early = crosswalk.visit_folder(
            "MRN-A", "U1", "C1", "2021-03-02T06:00:00-08:00"
        )
        late = crosswalk.visit_folder(
            "MRN-A", "U2", "C2", "2021-06-02T06:00:00-08:00"
        )
        assert (early, late) == ("visit-01", "visit-02")

    def test_a_later_visit_that_predates_one_already_numbered_warns(
        self, tmp_path, caplog
    ):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        crosswalk.visit_folder("MRN-A", "U2", "C2", "2021-06-02T06:00:00-08:00")
        with caplog.at_level("WARNING"):
            folder = crosswalk.visit_folder(
                "MRN-A", "U1", "C1", "2021-03-02T06:00:00-08:00"
            )
        assert folder == "visit-02"
        assert "earlier than one already numbered" in caplog.text

    def test_an_out_of_order_visit_in_another_patient_does_not_warn(
        self, tmp_path, caplog
    ):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        crosswalk.visit_folder("MRN-A", "U2", "C2", "2021-06-02T06:00:00-08:00")
        with caplog.at_level("WARNING"):
            crosswalk.visit_folder("MRN-B", "U1", "C1", "2021-03-02T06:00:00-08:00")
        assert "earlier than one already numbered" not in caplog.text

    def test_a_reloaded_visit_keeps_its_ordinal(self, tmp_path):
        path = tmp_path / "cw.csv"
        crosswalk = Crosswalk(path)
        visit = crosswalk.visit_folder("MRN-A", "U1", "C1")
        for exam_type, accession in (("us", "U1"), ("ct", "C1")):
            record(
                crosswalk,
                "MRN-A",
                accession,
                exam_type=exam_type,
                visit_folder=visit,
            )
        resumed = Crosswalk(path)
        assert resumed.visit_folder("MRN-A", "U1", "C1") == "visit-01"

    def test_a_visit_added_after_a_reload_continues_the_numbering(self, tmp_path):
        path = tmp_path / "cw.csv"
        crosswalk = Crosswalk(path)
        visit = crosswalk.visit_folder("MRN-A", "U1", "C1")
        for exam_type, accession in (("us", "U1"), ("ct", "C1")):
            record(
                crosswalk,
                "MRN-A",
                accession,
                exam_type=exam_type,
                visit_folder=visit,
            )
        resumed = Crosswalk(path)
        assert resumed.visit_folder("MRN-A", "U2", "C2") == "visit-02"

    def test_an_unparseable_timestamp_still_gets_an_ordinal(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        assert crosswalk.visit_folder("MRN-A", "U1", "C1", "not-a-date") == "visit-01"


class TestLeadingZeros:
    """A leading zero is part of an identifier, and must survive intact."""

    def test_the_real_mrn_keeps_its_leading_zeros(self, tmp_path):
        path = tmp_path / "cw.csv"
        record(Crosswalk(path), "00123456", "0099")
        assert "00123456,0099," in path.read_text()

    def test_a_leading_zero_mrn_resumes_correctly(self, tmp_path):
        path = tmp_path / "cw.csv"
        record(Crosswalk(path), "00123456", "0099")
        assert Crosswalk(path).patient_id("00123456") == "P0001"

    def test_two_mrns_differing_only_by_a_leading_zero_are_two_patients(
        self, tmp_path
    ):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        assert crosswalk.patient_id("00123456") != crosswalk.patient_id("123456")

    def test_two_mrns_differing_only_by_a_leading_zero_warn(self, tmp_path, caplog):
        # Legal, but far more often a zero lost to Excel upstream.
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        crosswalk.patient_id("00123456")
        with caplog.at_level("WARNING"):
            crosswalk.patient_id("123456")
        assert "differ only by leading zeros" in caplog.text

    def test_the_warning_does_not_name_the_mrns(self, tmp_path, caplog):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        crosswalk.patient_id("00123456")
        with caplog.at_level("WARNING"):
            crosswalk.patient_id("123456")
        assert "123456" not in caplog.text

    def test_two_accessions_differing_only_by_a_leading_zero_warn(
        self, tmp_path, caplog
    ):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        crosswalk.exam_id("MRN-A", "0099")
        with caplog.at_level("WARNING"):
            crosswalk.exam_id("MRN-A", "99")
        assert "differ only by leading zeros" in caplog.text

    def test_a_numeric_identifier_is_stored_as_text(self, tmp_path):
        # A JSON number arriving as a float must not become "123456.0".
        path = tmp_path / "cw.csv"
        record(Crosswalk(path), 123456.0, 99)
        (row,) = read_rows(path)
        assert row["mrn"] == "123456"
        assert row["accession_number"] == "99"


class TestParseAnonIds:
    """The reader of a cohort path lives beside its writer, and matches it."""

    def test_reads_the_cohort_layout(self):
        assert parse_anon_ids("P0001/visit-01/us/A0001.zip") == ("P0001", "A0001")

    def test_reads_an_extracted_directory(self):
        assert parse_anon_ids("P0001/visit-01/us/A0001") == ("P0001", "A0001")

    def test_a_visit_directory_has_no_accession(self):
        assert parse_anon_ids("P0001/visit-01/us") == ("P0001", "")

    def test_a_flat_archive_yields_no_ids(self):
        assert parse_anon_ids("A0001.zip") == ("", "")

    def test_an_unrecognised_path_yields_no_ids(self):
        assert parse_anon_ids("US-A.zip") == ("", "")

    def test_a_real_accession_shaped_like_an_anon_id_is_not_claimed(self):
        # Some sites really do issue "A0001". Without the P-component guard
        # that would land in a column named anon_accession_number.
        assert parse_anon_ids("some-cohort/A0001.zip") == ("", "")

    def test_a_path_object_is_accepted(self):
        assert parse_anon_ids(Path("P0002/visit-03/ct/A0009.zip")) == (
            "P0002",
            "A0009",
        )

    def test_round_trips_what_the_crosswalk_assigns(self, tmp_path):
        crosswalk = Crosswalk(tmp_path / "cw.csv")
        anon_mrn = crosswalk.patient_id("MRN-A")
        anon_accession = crosswalk.exam_id("MRN-A", "ACC-1")
        visit = crosswalk.visit_folder("MRN-A", "ACC-1", "ACC-2")
        archive = f"{anon_mrn}/{visit}/us/{anon_accession}.zip"
        assert parse_anon_ids(archive) == (anon_mrn, anon_accession)


class TestDefaultPath:
    """The crosswalk is a sibling of the cohort, never a child of it."""

    def test_named_for_the_output_directory(self):
        assert default_crosswalk_path("output-cohort") == Path(
            "output-cohort_crosswalk.csv"
        )

    def test_a_trailing_slash_does_not_change_the_name(self):
        assert default_crosswalk_path("output-cohort/") == Path(
            "output-cohort_crosswalk.csv"
        )

    def test_it_is_not_inside_the_output(self, tmp_path):
        output = tmp_path / "cohort"
        assert not default_crosswalk_path(output).is_relative_to(output)

    def test_a_nameless_output_falls_back(self):
        assert default_crosswalk_path(".") == Path("cohort_crosswalk.csv")


class TestNoIdentifiersAreLogged:
    """Counts may be logged; identifiers may not."""

    def test_identifiers_are_not_logged(self, tmp_path, caplog):
        path = tmp_path / "cw.csv"
        with caplog.at_level("DEBUG"):
            crosswalk = Crosswalk(path)
            record(crosswalk, "SECRET-MRN", "SECRET-ACC")
            Crosswalk(path)
        assert "SECRET-MRN" not in caplog.text
        assert "SECRET-ACC" not in caplog.text
