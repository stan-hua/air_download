"""Tests for counting frames in downloaded DICOMs and pruning by them.

Every DICOM here is synthesised in tmp_path with made-up identifiers, so no
test reads a real archive.
"""

# Standard libraries
import csv
import zipfile
from pathlib import Path

# Non-standard libraries
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

# Custom libraries
from air_download.frames import (
    EXAM_CSV_HEADER,
    FRAME_CSV_HEADER,
    inspect,
    iter_instances,
    prune,
    summarise_by_exam,
)

# A minimal ultrasound SOP class, enough for pydicom to read the header back.
_US_MULTIFRAME = "1.2.840.10008.5.1.4.1.1.3.1"


def make_dicom(
    path: Path,
    n_frames: int = 1,
    description: str = "RUQ",
    mrn: str = "Z1",
    accession: str = "US-A",
    modality: str = "US",
) -> Path:
    """Write a synthetic single-instance DICOM with a given frame count."""
    ds = Dataset()
    ds.PatientID = mrn
    ds.AccessionNumber = accession
    ds.Modality = modality
    ds.SeriesDescription = description
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = _US_MULTIFRAME
    ds.Rows = 480
    ds.Columns = 640
    if n_frames > 1:
        ds.NumberOfFrames = n_frames

    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(path, enforce_file_format=True)
    return path


def make_archive(path: Path, frames: list[int], accession: str = "US-A") -> Path:
    """Zip up one synthetic instance per entry in ``frames``."""
    staging = path.parent / f"_staging_{path.stem}"
    staging.mkdir(parents=True, exist_ok=True)
    members = [
        make_dicom(
            staging / f"IMG{index:04d}.dcm",
            n_frames=n,
            description=f"VIEW{index}",
            accession=accession,
        )
        for index, n in enumerate(frames)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for member in members:
            zf.write(member, arcname=member.name)
    return path


def read_rows(path: Path) -> list[dict]:
    """Read a written CSV back."""
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class TestIterInstances:
    """Walking archives and loose files."""

    def test_reads_every_member_of_an_archive(self, tmp_path):
        make_archive(tmp_path / "cohort" / "US-A.zip", [148, 1, 72])
        found = list(iter_instances(tmp_path / "cohort"))
        assert len(found) == 3

    def test_reads_a_single_zip(self, tmp_path):
        archive = make_archive(tmp_path / "US-A.zip", [148, 1])
        assert len(list(iter_instances(archive))) == 2

    def test_reads_loose_files(self, tmp_path):
        loose = tmp_path / "extracted"
        make_dicom(loose / "IMG0001.dcm", n_frames=148)
        make_dicom(loose / "IMG0002.dcm", n_frames=1)
        assert len(list(iter_instances(loose))) == 2

    def test_skips_non_dicom_members(self, tmp_path):
        # An includeLog download drops a spreadsheet into the archive.
        archive = tmp_path / "US-A.zip"
        make_archive(archive, [148])
        with zipfile.ZipFile(archive, "a") as zf:
            zf.writestr("download_log.xlsx", b"not a dicom")
            zf.writestr("DICOMDIR", b"not a dicom either")
        assert len(list(iter_instances(archive))) == 1

    def test_a_corrupt_archive_does_not_end_the_walk(self, tmp_path, caplog):
        root = tmp_path / "cohort"
        make_archive(root / "good.zip", [148])
        (root / "bad.zip").write_bytes(b"not a zip")
        with caplog.at_level("ERROR"):
            found = list(iter_instances(root))
        assert len(found) == 1
        assert "could not be opened" in caplog.text


class TestInspect:
    """The per-instance and per-exam reports."""

    def test_reports_frame_counts(self, tmp_path):
        make_archive(tmp_path / "cohort" / "US-A.zip", [148, 1, 72])
        out = tmp_path / "frames.csv"
        inspect(tmp_path / "cohort", output=out, min_frames=60)
        rows = read_rows(out)
        assert sorted(int(r["n_frames"]) for r in rows) == [1, 72, 148]
        assert list(rows[0]) == FRAME_CSV_HEADER

    def test_single_frame_images_count_as_one(self, tmp_path):
        # NumberOfFrames is absent on a still, not zero.
        make_archive(tmp_path / "cohort" / "US-A.zip", [1])
        out = tmp_path / "frames.csv"
        inspect(tmp_path / "cohort", output=out)
        (row,) = read_rows(out)
        assert row["n_frames"] == "1"

    def test_passes_column_reflects_the_threshold(self, tmp_path):
        make_archive(tmp_path / "cohort" / "US-A.zip", [148, 1, 72])
        out = tmp_path / "frames.csv"
        inspect(tmp_path / "cohort", output=out, min_frames=60)
        passing = {int(r["n_frames"]) for r in read_rows(out) if r["passes"] == "True"}
        assert passing == {72, 148}

    def test_every_instance_is_reported_regardless_of_threshold(self, tmp_path):
        # Inspect never filters: the point is to see the distribution.
        make_archive(tmp_path / "cohort" / "US-A.zip", [148, 1, 72])
        out = tmp_path / "frames.csv"
        inspect(tmp_path / "cohort", output=out, min_frames=1000)
        assert len(read_rows(out)) == 3

    def test_writes_the_per_exam_companion(self, tmp_path):
        make_archive(tmp_path / "cohort" / "US-A.zip", [148, 1, 72])
        out = tmp_path / "frames.csv"
        inspect(tmp_path / "cohort", output=out, min_frames=60)
        (row,) = read_rows(tmp_path / "frames_exams.csv")
        assert list(row) == EXAM_CSV_HEADER
        assert row["n_instances"] == "3"
        assert row["n_passing"] == "2"
        assert row["max_frames"] == "148"
        assert row["total_frames"] == "221"

    def test_flags_an_exam_with_no_qualifying_clip(self, tmp_path, caplog):
        root = tmp_path / "cohort"
        make_archive(root / "US-A.zip", [148], accession="US-A")
        make_archive(root / "US-B.zip", [1, 2], accession="US-B")
        with caplog.at_level("WARNING"):
            inspect(root, output=tmp_path / "frames.csv", min_frames=60)
        assert "1 of 2 archive(s) have no instance" in caplog.text
        empty = [
            r for r in read_rows(tmp_path / "frames_exams.csv") if r["n_passing"] == "0"
        ]
        assert len(empty) == 1

    def test_identifiers_are_not_logged(self, tmp_path, caplog):
        root = tmp_path / "cohort"
        make_archive(root / "US-B.zip", [1], accession="SECRET-ACC")
        with caplog.at_level("DEBUG"):
            inspect(root, output=tmp_path / "frames.csv", min_frames=60)
        assert "SECRET-ACC" not in caplog.text
        assert "Z1" not in caplog.text

    def test_empty_input_writes_nothing(self, tmp_path, caplog):
        (tmp_path / "empty").mkdir()
        out = tmp_path / "frames.csv"
        with caplog.at_level("WARNING"):
            inspect(tmp_path / "empty", output=out)
        assert not out.exists()
        assert "no dicom instances found" in caplog.text.lower()

    def test_rejects_a_zero_threshold(self, tmp_path):
        with pytest.raises(ValueError, match="min_frames must be at least 1"):
            inspect(tmp_path, min_frames=0)

    def test_missing_input_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            inspect(tmp_path / "nope")


class TestModalityExemption:
    """The frame threshold only means something where an object holds many."""

    def test_a_ct_slice_passes_below_the_threshold(self, tmp_path):
        # One CT object is one slice. Judging it on frames rejects every
        # slice, and prune then deletes the whole series.
        root = tmp_path / "cohort"
        make_dicom(root / "loose" / "CT0000.dcm", n_frames=1, modality="CT")
        out = tmp_path / "frames.csv"
        inspect(root, output=out, min_frames=60)
        (row,) = read_rows(out)
        assert row["passes"] == "True"

    def test_a_short_ultrasound_clip_still_fails(self, tmp_path):
        root = tmp_path / "cohort"
        make_dicom(root / "loose" / "US0000.dcm", n_frames=5, modality="US")
        out = tmp_path / "frames.csv"
        inspect(root, output=out, min_frames=60)
        (row,) = read_rows(out)
        assert row["passes"] == "False"

    def test_an_unknown_modality_passes(self, tmp_path):
        # Never discard what cannot be classified.
        root = tmp_path / "cohort"
        make_dicom(root / "loose" / "X0000.dcm", n_frames=1, modality="")
        out = tmp_path / "frames.csv"
        inspect(root, output=out, min_frames=60)
        (row,) = read_rows(out)
        assert row["passes"] == "True"

    def test_an_empty_modality_list_applies_the_threshold_to_all(self, tmp_path):
        root = tmp_path / "cohort"
        make_dicom(root / "loose" / "CT0000.dcm", n_frames=1, modality="CT")
        out = tmp_path / "frames.csv"
        inspect(root, output=out, min_frames=60, min_frames_modalities="")
        (row,) = read_rows(out)
        assert row["passes"] == "False"

    def test_the_modality_list_is_case_insensitive(self, tmp_path):
        root = tmp_path / "cohort"
        make_dicom(root / "loose" / "US0000.dcm", n_frames=5, modality="US")
        out = tmp_path / "frames.csv"
        inspect(root, output=out, min_frames=60, min_frames_modalities=" us ")
        (row,) = read_rows(out)
        assert row["passes"] == "False"

    def test_prune_keeps_a_ct_whole(self, tmp_path):
        # The bug this exists to stop: prune deleting every CT in a cohort.
        root = tmp_path / "cohort"
        ct = root / "P0001" / "visit-01" / "ct" / "A0002.zip"
        staging = tmp_path / "staging"
        members = [
            make_dicom(staging / f"CT{i:04d}.dcm", n_frames=1, modality="CT")
            for i in range(3)
        ]
        ct.parent.mkdir(parents=True)
        with zipfile.ZipFile(ct, "w") as zf:
            for member in members:
                zf.write(member, arcname=member.name)

        prune(root, tmp_path / "pruned", min_frames=60)

        out = tmp_path / "pruned" / "P0001" / "visit-01" / "ct" / "A0002.zip"
        assert out.exists()
        with zipfile.ZipFile(out) as zf:
            assert len(zf.namelist()) == 3

    def test_prune_still_drops_a_short_ultrasound_clip(self, tmp_path):
        root = tmp_path / "cohort"
        make_archive(root / "P0001" / "visit-01" / "us" / "A0001.zip", [148, 5])
        prune(root, tmp_path / "pruned", min_frames=60)
        with zipfile.ZipFile(
            tmp_path / "pruned" / "P0001" / "visit-01" / "us" / "A0001.zip"
        ) as zf:
            assert len(zf.namelist()) == 1


class TestAnonColumns:
    """Identifiers come from the path. The header is never read for one."""

    def test_ids_come_from_the_path_not_the_header(self, tmp_path):
        root = tmp_path / "cohort"
        make_archive(
            root / "P0001" / "visit-01" / "us" / "A0001.zip",
            [148],
            accession="SECRET-ACC",
        )
        out = tmp_path / "frames.csv"
        inspect(root, output=out, min_frames=60)

        (row,) = read_rows(out)
        assert row["anon_mrn"] == "P0001"
        assert row["anon_accession_number"] == "A0001"

    def test_header_identifiers_never_reach_the_csv(self, tmp_path):
        # make_dicom writes PatientID and AccessionNumber. With no
        # anonymization profile those are the real values, which is exactly
        # why nothing reads them any more.
        root = tmp_path / "cohort"
        staging = tmp_path / "staging"
        member = make_dicom(
            staging / "IMG0000.dcm",
            n_frames=148,
            mrn="SECRET-MRN",
            accession="SECRET-ACC",
        )
        archive = root / "P0001" / "visit-01" / "us" / "A0001.zip"
        archive.parent.mkdir(parents=True)
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(member, arcname=member.name)

        out = tmp_path / "frames.csv"
        inspect(root, output=out, min_frames=60)

        written = out.read_text() + (tmp_path / "frames_exams.csv").read_text()
        assert "SECRET-MRN" not in written
        assert "SECRET-ACC" not in written

    def test_a_non_cohort_path_yields_empty_anon_columns(self, tmp_path):
        # Better empty than a guess: some sites issue real accessions that
        # look exactly like a generated one.
        make_archive(tmp_path / "cohort" / "A0001.zip", [148])
        out = tmp_path / "frames.csv"
        inspect(tmp_path / "cohort", output=out, min_frames=60)

        (row,) = read_rows(out)
        assert row["anon_mrn"] == ""
        assert row["anon_accession_number"] == ""

    def test_a_tree_outside_the_layout_is_reported(self, tmp_path, caplog):
        make_archive(tmp_path / "cohort" / "US-A.zip", [148])
        with caplog.at_level("INFO"):
            inspect(tmp_path / "cohort", output=tmp_path / "frames.csv")
        assert "not in the pseudonymous cohort layout" in caplog.text

    def test_the_uid_columns_are_absent(self):
        assert "series_uid" not in FRAME_CSV_HEADER
        assert "sop_instance_uid" not in FRAME_CSV_HEADER

    def test_the_member_name_is_absent(self):
        # An AIR download names members <studyUid>/<seriesUid>/<sopUid>.dcm,
        # so writing the name would put the dropped UIDs back in the CSV.
        assert "member" not in FRAME_CSV_HEADER
        assert "member_index" in FRAME_CSV_HEADER

    def test_uid_shaped_member_names_never_reach_the_csv(self, tmp_path):
        root = tmp_path / "cohort"
        staging = tmp_path / "staging"
        member = make_dicom(staging / "IMG0000.dcm", n_frames=148)
        uid_name = (
            "1.3.6.1.4.1.37209.11111/1.3.6.1.4.1.37209.22222/"
            "1.3.6.1.4.1.37209.33333.dcm"
        )
        archive = root / "P0001" / "visit-01" / "us" / "A0001.zip"
        archive.parent.mkdir(parents=True)
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(member, arcname=uid_name)

        out = tmp_path / "frames.csv"
        inspect(root, output=out, min_frames=60)
        assert "1.3.6.1.4.1.37209" not in out.read_text()

    def test_the_member_index_locates_the_member(self, tmp_path):
        # The index must be usable against namelist() to be worth writing.
        root = tmp_path / "cohort"
        archive = make_archive(
            root / "P0001" / "visit-01" / "us" / "A0001.zip", [148, 1, 72]
        )
        out = tmp_path / "frames.csv"
        inspect(root, output=out, min_frames=60)

        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        for row in read_rows(out):
            assert names[int(row["member_index"])].startswith("IMG")

    def test_the_index_skips_over_a_non_dicom_member(self, tmp_path):
        # It counts every member, including the ones inspect ignores, or it
        # would not index back into namelist().
        root = tmp_path / "cohort"
        archive = root / "P0001" / "visit-01" / "us" / "A0001.zip"
        member = make_dicom(tmp_path / "staging" / "IMG0000.dcm", n_frames=148)
        archive.parent.mkdir(parents=True)
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("download_log.xlsx", "not dicom")
            zf.write(member, arcname="IMG0000.dcm")

        out = tmp_path / "frames.csv"
        inspect(root, output=out, min_frames=60)
        (row,) = read_rows(out)
        assert row["member_index"] == "1"

    def test_the_real_identifier_columns_are_absent(self):
        for header in (FRAME_CSV_HEADER, EXAM_CSV_HEADER):
            assert "mrn" not in header
            assert "accession_number" not in header


class TestSummariseByExam:
    """The rollup, independent of any file."""

    def test_groups_by_archive(self):
        rows = [
            {"archive": "a.zip", "n_frames": 100, "passes": True},
            {"archive": "a.zip", "n_frames": 1, "passes": False},
            {"archive": "b.zip", "n_frames": 80, "passes": True},
        ]
        summaries = summarise_by_exam(rows)
        assert [s["archive"] for s in summaries] == ["a.zip", "b.zip"]
        assert summaries[0]["n_instances"] == 2
        assert summaries[0]["n_passing"] == 1
        assert summaries[0]["total_frames"] == 101

    def test_ids_come_from_the_archive_path(self):
        # One rule for both CSVs: the path, never a value carried up a row.
        rows = [
            {"archive": "P0001/visit-01/us/A0001.zip", "n_frames": 1,
             "passes": False},
        ]
        (summary,) = summarise_by_exam(rows)
        assert summary["anon_mrn"] == "P0001"
        assert summary["anon_accession_number"] == "A0001"

    def test_a_non_cohort_archive_has_no_ids(self):
        rows = [{"archive": "a.zip", "n_frames": 1, "passes": False}]
        (summary,) = summarise_by_exam(rows)
        assert summary["anon_mrn"] == ""
        assert summary["anon_accession_number"] == ""


class TestPrune:
    """Writing a filtered copy, never touching the source."""

    def test_keeps_only_qualifying_instances(self, tmp_path):
        make_archive(tmp_path / "cohort" / "US-A.zip", [148, 1, 72])
        prune(tmp_path / "cohort", tmp_path / "pruned", min_frames=60)
        with zipfile.ZipFile(tmp_path / "pruned" / "US-A.zip") as zf:
            assert len(zf.namelist()) == 2

    def test_leaves_the_source_untouched(self, tmp_path):
        source = make_archive(tmp_path / "cohort" / "US-A.zip", [148, 1, 72])
        before = source.read_bytes()
        prune(tmp_path / "cohort", tmp_path / "pruned", min_frames=60)
        assert source.read_bytes() == before

    def test_mirrors_the_directory_layout(self, tmp_path):
        layout = Path("P0001") / "visit-01" / "us" / "A0001.zip"
        make_archive(tmp_path / "cohort" / layout, [148])
        prune(tmp_path / "cohort", tmp_path / "pruned", min_frames=60)
        assert (tmp_path / "pruned" / layout).exists()

    def test_archive_with_nothing_qualifying_is_not_written(self, tmp_path, caplog):
        root = tmp_path / "cohort"
        make_archive(root / "US-A.zip", [148], accession="US-A")
        make_archive(root / "US-B.zip", [1, 2], accession="US-B")
        with caplog.at_level("WARNING"):
            prune(root, tmp_path / "pruned", min_frames=60)
        assert (tmp_path / "pruned" / "US-A.zip").exists()
        assert not (tmp_path / "pruned" / "US-B.zip").exists()
        assert "1 archive(s) had no instance" in caplog.text

    def test_refuses_to_write_inside_its_input(self, tmp_path):
        root = tmp_path / "cohort"
        make_archive(root / "US-A.zip", [148])
        with pytest.raises(ValueError, match="is inside input"):
            prune(root, root / "pruned", min_frames=60)

    def test_rejects_a_zero_threshold(self, tmp_path):
        with pytest.raises(ValueError, match="min_frames must be at least 1"):
            prune(tmp_path, tmp_path / "out", min_frames=0)

    def test_pruned_output_is_readable_again(self, tmp_path):
        # The round trip matters: a pruned tree must still inspect cleanly.
        make_archive(tmp_path / "cohort" / "US-A.zip", [148, 1, 72])
        prune(tmp_path / "cohort", tmp_path / "pruned", min_frames=60)
        out = tmp_path / "frames.csv"
        inspect(tmp_path / "pruned", output=out, min_frames=60)
        assert all(r["passes"] == "True" for r in read_rows(out))
