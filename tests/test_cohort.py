"""Tests for the matched-cohort downloader.

All identifiers here are synthetic, and the AIR client is replaced with a
stub, so no test touches the network or a real accession number.
"""

# Standard libraries
import csv
from pathlib import Path

# Non-standard libraries
import pytest

# Custom libraries
from air_download.us_ct import cohort
from air_download.us_ct.cohort import (
    _safe_component,
    build_visit_paths,
    download_cohort,
    read_matched_pairs,
    visit_folder_name,
)

HEADER = ["mrn", "us_accession_number", "us_date_time", "ct_accession_number"]


def write_csv(path: Path, rows: list[list[str]], header: list[str] = HEADER) -> Path:
    """Write a synthetic matched-pairs CSV."""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return path


class StubClient:
    """Records download() calls and writes a placeholder archive."""

    instances: list["StubClient"] = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list[dict] = []
        StubClient.instances.append(self)

    def download(self, **kwargs):
        self.calls.append(kwargs)
        output = kwargs["output"]
        output.write_bytes(b"zip-bytes")
        return None


@pytest.fixture
def stub_client(monkeypatch):
    """Replace AIRClient in the cohort module with the recording stub."""
    StubClient.instances = []
    monkeypatch.setattr(cohort, "AIRClient", StubClient)
    yield StubClient


def row(mrn="A1", us="U1", when="2021-03-02T06:00:00-08:00", ct="C1"):
    """Build a matched-pair row."""
    return {
        "mrn": mrn,
        "us_accession_number": us,
        "us_date_time": when,
        "ct_accession_number": ct,
    }


class TestVisitFolderName:
    def test_formats_us_date_as_mm_dd_yy(self):
        assert visit_folder_name("2021-03-02T06:00:00-08:00", "U1", "C1") == "03-02-21"

    def test_date_only_timestamp(self):
        assert visit_folder_name("2021-12-25", "U1", "C1") == "12-25-21"

    def test_offset_is_respected_not_shifted(self):
        # 23:30 on the 2nd at -08:00 stays the 2nd, not the 3rd in UTC.
        assert visit_folder_name("2021-03-02T23:30:00-08:00", "U1", "C1") == "03-02-21"

    def test_unparseable_falls_back_to_accession_pair(self):
        assert visit_folder_name("not-a-date", "U1", "C1") == "U1_C1"

    def test_empty_falls_back_to_accession_pair(self):
        assert visit_folder_name("", "U1", "C1") == "U1_C1"


class TestBuildVisitPaths:
    def test_layout(self, tmp_path):
        us_path, ct_path = build_visit_paths(tmp_path, row())
        assert us_path == tmp_path / "A1" / "03-02-21" / "us" / "U1.zip"
        assert ct_path == tmp_path / "A1" / "03-02-21" / "ct" / "C1.zip"

    def test_folder_follows_the_us_when_ct_is_the_next_day(self, tmp_path):
        # The CT crossed midnight; the visit is still named for the FAST.
        us_path, ct_path = build_visit_paths(
            tmp_path, row(when="2021-03-02T23:00:00-08:00")
        )
        assert us_path.parent.parent.name == "03-02-21"
        assert ct_path.parent.parent.name == "03-02-21"

    def test_same_patient_same_day_different_pair_gets_suffix(self, tmp_path):
        claimed = {}
        first, _ = build_visit_paths(tmp_path, row(us="U1", ct="C1"), claimed)
        second, _ = build_visit_paths(tmp_path, row(us="U2", ct="C2"), claimed)
        assert first.parent.parent.name == "03-02-21"
        assert second.parent.parent.name == "03-02-21_2"

    def test_same_pair_twice_keeps_one_folder(self, tmp_path):
        claimed = {}
        first, _ = build_visit_paths(tmp_path, row(), claimed)
        second, _ = build_visit_paths(tmp_path, row(), claimed)
        assert first == second

    def test_different_patients_same_day_do_not_collide(self, tmp_path):
        claimed = {}
        first, _ = build_visit_paths(tmp_path, row(mrn="A1"), claimed)
        second, _ = build_visit_paths(tmp_path, row(mrn="A2"), claimed)
        assert first.parent.parent.name == second.parent.parent.name == "03-02-21"
        assert first != second


class TestSafeComponent:
    def test_parent_traversal_neutralized(self):
        # The separator goes, so what is left is one literal segment rather
        # than a step up the tree.
        safe = _safe_component("../etc")
        assert safe == ".._etc"
        assert Path("root", safe).parent == Path("root")

    def test_separators_replaced(self):
        assert _safe_component("A/1") == "A_1"

    def test_dot_only_value_replaced(self):
        assert _safe_component("..") == "_"

    def test_ordinary_value_untouched(self):
        assert _safe_component("A1-2_3") == "A1-2_3"


class TestReadMatchedPairs:
    def test_reads_required_columns(self, tmp_path):
        path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        assert read_matched_pairs(path) == [
            {
                "mrn": "A1",
                "us_accession_number": "U1",
                "us_date_time": "2021-03-02",
                "ct_accession_number": "C1",
            }
        ]

    def test_extra_columns_ignored(self, tmp_path):
        path = write_csv(
            tmp_path / "m.csv",
            [["A1", "U1", "2021-03-02", "US ED", "C1", "4.0"]],
            header=[
                "mrn",
                "us_accession_number",
                "us_date_time",
                "us_description",
                "ct_accession_number",
                "hours_between",
            ],
        )
        assert len(read_matched_pairs(path)) == 1

    def test_column_order_does_not_matter(self, tmp_path):
        path = write_csv(
            tmp_path / "m.csv",
            [["C1", "A1", "2021-03-02", "U1"]],
            header=[
                "ct_accession_number",
                "mrn",
                "us_date_time",
                "us_accession_number",
            ],
        )
        assert read_matched_pairs(path)[0]["us_accession_number"] == "U1"

    def test_exact_duplicates_collapsed(self, tmp_path):
        path = write_csv(
            tmp_path / "m.csv",
            [["A1", "U1", "2021-03-02", "C1"], ["A1", "U1", "2021-03-02", "C1"]],
        )
        assert len(read_matched_pairs(path)) == 1

    def test_same_ct_two_ultrasounds_both_kept(self, tmp_path):
        path = write_csv(
            tmp_path / "m.csv",
            [["A1", "U1", "2021-03-02", "C1"], ["A1", "U2", "2021-03-02", "C1"]],
        )
        assert len(read_matched_pairs(path)) == 2

    def test_whitespace_stripped(self, tmp_path):
        path = write_csv(tmp_path / "m.csv", [[" A1 ", " U1 ", "2021-03-02", " C1 "]])
        assert read_matched_pairs(path)[0]["mrn"] == "A1"

    @pytest.mark.parametrize(
        "bad_row",
        [
            ["", "U1", "2021-03-02", "C1"],
            ["A1", "", "2021-03-02", "C1"],
            ["A1", "U1", "", "C1"],
            ["A1", "U1", "2021-03-02", ""],
        ],
    )
    def test_incomplete_rows_skipped(self, tmp_path, bad_row):
        path = write_csv(
            tmp_path / "m.csv", [bad_row, ["A2", "U2", "2021-03-05", "C2"]]
        )
        assert len(read_matched_pairs(path)) == 1

    def test_missing_column_raises_naming_what_was_found(self, tmp_path):
        path = write_csv(
            tmp_path / "m.csv",
            [["A1", "U1"]],
            header=["mrn", "us_accession_number"],
        )
        with pytest.raises(ValueError) as exc:
            read_matched_pairs(path)
        assert "us_date_time" in str(exc.value)
        assert "ct_accession_number" in str(exc.value)
        assert "us_accession_number" in str(exc.value)

    def test_empty_file_returns_empty(self, tmp_path):
        path = write_csv(tmp_path / "m.csv", [])
        assert read_matched_pairs(path) == []


class TestDownloadCohort:
    def test_downloads_both_exams_into_the_visit(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        download_cohort(matched_csv=csv_path, output=out, cred_path=None)

        client = stub_client.instances[0]
        assert len(client.calls) == 2
        assert (out / "A1" / "03-02-21" / "us" / "U1.zip").exists()
        assert (out / "A1" / "03-02-21" / "ct" / "C1.zip").exists()

    def test_thinnest_axial_only_on_the_ct(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        download_cohort(matched_csv=csv_path, output=tmp_path / "out")

        us_call, ct_call = stub_client.instances[0].calls
        assert us_call["accession"] == "U1"
        assert us_call["thinnest_axial"] is False
        assert ct_call["accession"] == "C1"
        assert ct_call["thinnest_axial"] is True

    def test_every_call_carries_the_mrn(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        download_cohort(matched_csv=csv_path, output=tmp_path / "out")
        assert all(c["mrn"] == "A1" for c in stub_client.instances[0].calls)

    def test_n_limits_the_rows(self, tmp_path, stub_client):
        csv_path = write_csv(
            tmp_path / "m.csv",
            [
                ["A1", "U1", "2021-03-02", "C1"],
                ["A2", "U2", "2021-03-05", "C2"],
                ["A3", "U3", "2021-03-08", "C3"],
            ],
        )
        out = tmp_path / "out"
        download_cohort(matched_csv=csv_path, output=out, n=1)

        assert len(stub_client.instances[0].calls) == 2
        assert (out / "A1").exists()
        assert not (out / "A2").exists()

    def test_n_below_one_raises(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        with pytest.raises(ValueError):
            download_cohort(matched_csv=csv_path, output=tmp_path / "out", n=0)

    def test_skip_existing_leaves_present_archive_alone(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        us_path = out / "A1" / "03-02-21" / "us" / "U1.zip"
        us_path.parent.mkdir(parents=True)
        us_path.write_bytes(b"already-here")

        download_cohort(matched_csv=csv_path, output=out)

        calls = stub_client.instances[0].calls
        assert [c["accession"] for c in calls] == ["C1"]
        assert us_path.read_bytes() == b"already-here"

    def test_empty_archive_is_not_treated_as_present(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        us_path = out / "A1" / "03-02-21" / "us" / "U1.zip"
        us_path.parent.mkdir(parents=True)
        us_path.touch()

        download_cohort(matched_csv=csv_path, output=out)

        assert [c["accession"] for c in stub_client.instances[0].calls] == ["U1", "C1"]

    def test_skip_existing_off_overwrites_in_place(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        us_path = out / "A1" / "03-02-21" / "us" / "U1.zip"
        us_path.parent.mkdir(parents=True)
        us_path.write_bytes(b"stale")

        download_cohort(matched_csv=csv_path, output=out, skip_existing=False)

        assert us_path.read_bytes() == b"zip-bytes"
        # No sibling left behind by the index-around-existing-file path.
        assert list(us_path.parent.iterdir()) == [us_path]

    def test_repeated_ct_is_copied_not_downloaded_twice(self, tmp_path, stub_client):
        csv_path = write_csv(
            tmp_path / "m.csv",
            [
                ["A1", "U1", "2021-03-02T06:00:00-08:00", "C1"],
                ["A1", "U2", "2021-03-03T06:00:00-08:00", "C1"],
            ],
        )
        out = tmp_path / "out"
        download_cohort(matched_csv=csv_path, output=out)

        accessions = [c["accession"] for c in stub_client.instances[0].calls]
        assert accessions == ["U1", "C1", "U2"]
        assert (out / "A1" / "03-02-21" / "ct" / "C1.zip").exists()
        assert (out / "A1" / "03-03-21" / "ct" / "C1.zip").exists()

    def test_failure_does_not_stop_the_run(self, tmp_path, stub_client, monkeypatch):
        csv_path = write_csv(
            tmp_path / "m.csv",
            [["A1", "U1", "2021-03-02", "C1"], ["A2", "U2", "2021-03-05", "C2"]],
        )
        out = tmp_path / "out"

        def flaky_download(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["accession"] == "U1":
                raise RuntimeError("boom")
            kwargs["output"].write_bytes(b"zip-bytes")

        monkeypatch.setattr(StubClient, "download", flaky_download)
        download_cohort(matched_csv=csv_path, output=out)

        assert (out / "A2" / "03-05-21" / "ct" / "C2.zip").exists()
        assert not (out / "A1" / "03-02-21" / "us" / "U1.zip").exists()
        assert (out / "A1" / "03-02-21" / "ct" / "C1.zip").exists()

    def test_exam_that_writes_nothing_counts_as_failed(
        self, tmp_path, stub_client, monkeypatch, caplog
    ):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])

        def no_match(self, **kwargs):
            # A search matching nothing returns without writing a file.
            self.calls.append(kwargs)

        monkeypatch.setattr(StubClient, "download", no_match)
        download_cohort(matched_csv=csv_path, output=tmp_path / "out")

        assert "2 failed" in caplog.text
        assert "2 exam(s) downloaded" not in caplog.text

    def test_dry_run_writes_nothing_and_builds_no_client(
        self, tmp_path, stub_client, capsys
    ):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        download_cohort(matched_csv=csv_path, output=out, dry_run=True)

        assert stub_client.instances == []
        assert not out.exists()
        printed = capsys.readouterr().out
        assert str(out / "A1" / "03-02-21" / "us" / "U1.zip") in printed
        assert str(out / "A1" / "03-02-21" / "ct" / "C1.zip") in printed

    def test_no_usable_rows_makes_no_client(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["", "U1", "2021-03-02", "C1"]])
        download_cohort(matched_csv=csv_path, output=tmp_path / "out")
        assert stub_client.instances == []


class TestLeadingZeroMrn:
    """The visit folder must keep an MRN's leading zero."""

    def test_folder_name_keeps_leading_zeros(self, tmp_path):
        us_path, ct_path = build_visit_paths(tmp_path, row(mrn="00123456"))
        assert us_path.parts[-4] == "00123456"
        assert ct_path.parts[-4] == "00123456"

    def test_read_matched_pairs_keeps_leading_zeros(self, tmp_path):
        path = write_csv(
            tmp_path / "matched.csv",
            [["00123456", "0099", "2021-03-02T06:00:00-08:00", "0100"]],
        )
        (parsed,) = read_matched_pairs(path)
        assert parsed["mrn"] == "00123456"
        assert parsed["us_accession_number"] == "0099"

    def test_two_mrns_differing_only_by_a_leading_zero_get_separate_folders(
        self, tmp_path
    ):
        claimed = {}
        first, _ = build_visit_paths(tmp_path, row(mrn="0123"), claimed)
        second, _ = build_visit_paths(tmp_path, row(mrn="123"), claimed)
        assert first.parts[-4] == "0123"
        assert second.parts[-4] == "123"
        assert first != second
