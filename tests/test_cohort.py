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
from air_download.crosswalk import Crosswalk
from air_download.us_ct import cohort
from air_download.us_ct.cohort import (
    _safe_component,
    build_visit_paths,
    download_cohort,
    read_matched_pairs,
    sort_rows_for_numbering,
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


def row(
    mrn="MRN-SECRET",
    us="US-SECRET",
    when="2021-03-02T06:00:00-08:00",
    ct="CT-SECRET",
):
    """Build a matched-pair row.

    The values are distinctive on purpose: several tests assert that none of
    them reaches a path or a log line, and a short value like "A1" would make
    those assertions pass without proving anything.
    """
    return {
        "mrn": mrn,
        "us_accession_number": us,
        "us_date_time": when,
        "ct_accession_number": ct,
    }


def crosswalk(tmp_path, name="cw.csv", **kwargs):
    """Build a crosswalk outside whatever output directory a test uses."""
    return Crosswalk(tmp_path / name, **kwargs)


class TestSortRowsForNumbering:
    """Visits are numbered chronologically within each patient."""

    def test_a_patients_rows_are_ordered_by_the_ultrasound(self):
        late = row(us="U2", ct="C2", when="2021-06-02T06:00:00-08:00")
        early = row(us="U1", ct="C1", when="2021-03-02T06:00:00-08:00")
        assert sort_rows_for_numbering([late, early]) == [early, late]

    def test_patients_keep_the_order_they_first_appear_in(self):
        second = row(mrn="B", when="2021-01-01T06:00:00-08:00")
        first = row(mrn="A", when="2021-09-01T06:00:00-08:00")
        ordered = sort_rows_for_numbering([first, second])
        assert [r["mrn"] for r in ordered] == ["A", "B"]

    def test_an_unparseable_timestamp_sorts_last_within_its_patient(self):
        undated = row(us="U0", ct="C0", when="not-a-date")
        dated = row(us="U1", ct="C1", when="2021-03-02T06:00:00-08:00")
        assert sort_rows_for_numbering([undated, dated]) == [dated, undated]

    def test_mixed_offsets_are_compared_as_instants(self):
        # 23:00 at -08:00 is later than 06:00 the next day at +09:00.
        earlier = row(us="U1", ct="C1", when="2021-03-03T06:00:00+09:00")
        later = row(us="U2", ct="C2", when="2021-03-02T23:00:00-08:00")
        assert sort_rows_for_numbering([later, earlier]) == [earlier, later]


class TestBuildVisitPaths:
    def test_layout_uses_anon_ids(self, tmp_path):
        out = tmp_path / "cohort"
        us_path, ct_path = build_visit_paths(out, row(), crosswalk(tmp_path))
        assert us_path == out / "P0001" / "visit-01" / "us" / "A0001.zip"
        assert ct_path == out / "P0001" / "visit-01" / "ct" / "A0002.zip"

    def test_no_real_identifier_appears_in_the_path(self, tmp_path):
        out = tmp_path / "cohort"
        paths = build_visit_paths(out, row(), crosswalk(tmp_path))
        for path in paths:
            assert "MRN-SECRET" not in str(path)
            assert "US-SECRET" not in str(path)
            assert "CT-SECRET" not in str(path)

    def test_the_date_does_not_appear_in_the_path(self, tmp_path):
        # The folder used to be the visit date, which is PHI under Safe Harbor.
        out = tmp_path / "cohort"
        us_path, _ = build_visit_paths(out, row(), crosswalk(tmp_path))
        assert "03-02-21" not in str(us_path)

    def test_two_visits_for_one_patient_get_distinct_ordinals(self, tmp_path):
        out = tmp_path / "cohort"
        shared = crosswalk(tmp_path)
        first, _ = build_visit_paths(out, row(us="U1", ct="C1"), shared)
        second, _ = build_visit_paths(out, row(us="U2", ct="C2"), shared)
        assert first.parent.parent.name == "visit-01"
        assert second.parent.parent.name == "visit-02"

    def test_two_pairs_on_the_same_day_no_longer_need_a_suffix(self, tmp_path):
        out = tmp_path / "cohort"
        shared = crosswalk(tmp_path)
        first, _ = build_visit_paths(out, row(us="U1", ct="C1"), shared)
        second, _ = build_visit_paths(out, row(us="U2", ct="C2"), shared)
        assert "_2" not in second.parent.parent.name
        assert first.parent.parent != second.parent.parent

    def test_same_pair_twice_keeps_one_folder(self, tmp_path):
        out = tmp_path / "cohort"
        shared = crosswalk(tmp_path)
        first, _ = build_visit_paths(out, row(), shared)
        second, _ = build_visit_paths(out, row(), shared)
        assert first == second

    def test_different_patients_do_not_collide(self, tmp_path):
        out = tmp_path / "cohort"
        shared = crosswalk(tmp_path)
        first, _ = build_visit_paths(out, row(mrn="A1"), shared)
        second, _ = build_visit_paths(out, row(mrn="A2"), shared)
        # Both are that patient's first visit, under different patients.
        assert first.parent.parent.name == second.parent.parent.name == "visit-01"
        assert first != second

    def test_the_us_and_ct_share_a_visit_folder(self, tmp_path):
        out = tmp_path / "cohort"
        us_path, ct_path = build_visit_paths(out, row(), crosswalk(tmp_path))
        assert us_path.parent.parent == ct_path.parent.parent


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
                # Optional, so a CSV predating the column still reads.
                "ct_date_time": "",
            }
        ]

    def test_the_ct_timestamp_is_read_when_present(self, tmp_path):
        path = write_csv(
            tmp_path / "m.csv",
            [["A1", "U1", "2021-03-02", "C1", "2021-03-02T09:00:00-08:00"]],
            header=HEADER + ["ct_date_time"],
        )
        (parsed,) = read_matched_pairs(path)
        assert parsed["ct_date_time"] == "2021-03-02T09:00:00-08:00"

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
        assert (out / "P0001" / "visit-01" / "us" / "A0001.zip").exists()
        assert (out / "P0001" / "visit-01" / "ct" / "A0002.zip").exists()

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
        assert (out / "P0001").exists()
        assert not (out / "P0002").exists()

    def test_n_below_one_raises(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        with pytest.raises(ValueError):
            download_cohort(matched_csv=csv_path, output=tmp_path / "out", n=0)

    def test_skip_existing_leaves_present_archive_alone(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        us_path = out / "P0001" / "visit-01" / "us" / "A0001.zip"
        us_path.parent.mkdir(parents=True)
        us_path.write_bytes(b"already-here")

        download_cohort(matched_csv=csv_path, output=out)

        calls = stub_client.instances[0].calls
        assert [c["accession"] for c in calls] == ["C1"]
        assert us_path.read_bytes() == b"already-here"

    def test_empty_archive_is_not_treated_as_present(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        us_path = out / "P0001" / "visit-01" / "us" / "A0001.zip"
        us_path.parent.mkdir(parents=True)
        us_path.touch()

        download_cohort(matched_csv=csv_path, output=out)

        assert [c["accession"] for c in stub_client.instances[0].calls] == ["U1", "C1"]

    def test_skip_existing_off_overwrites_in_place(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        us_path = out / "P0001" / "visit-01" / "us" / "A0001.zip"
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
        assert (out / "P0001" / "visit-01" / "ct" / "A0002.zip").exists()
        assert (out / "P0001" / "visit-02" / "ct" / "A0002.zip").exists()

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

        assert (out / "P0002" / "visit-01" / "ct" / "A0004.zip").exists()
        assert not (out / "P0001" / "visit-01" / "us" / "A0001.zip").exists()
        assert (out / "P0001" / "visit-01" / "ct" / "A0002.zip").exists()

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
        assert str(out / "P0001" / "visit-01" / "us" / "A0001.zip") in printed
        assert str(out / "P0001" / "visit-01" / "ct" / "A0002.zip") in printed

    def test_no_usable_rows_makes_no_client(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["", "U1", "2021-03-02", "C1"]])
        download_cohort(matched_csv=csv_path, output=tmp_path / "out")
        assert stub_client.instances == []


class TestTheCrosswalk:
    """The mapping is written beside the cohort, and drives resumption."""

    def test_it_lands_outside_the_output_tree(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        download_cohort(matched_csv=csv_path, output=out)

        written = tmp_path / "out_crosswalk.csv"
        assert written.exists()
        assert not written.is_relative_to(out)

    def test_a_crosswalk_inside_the_output_tree_is_refused(
        self, tmp_path, stub_client
    ):
        # Otherwise any copy of the cohort ships the key with the lock.
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        with pytest.raises(ValueError, match="inside the cohort"):
            download_cohort(
                matched_csv=csv_path, output=out, crosswalk_csv=out / "cw.csv"
            )

    def test_a_row_exists_before_the_download(self, tmp_path, stub_client, monkeypatch):
        # An archive on disk with no way back cannot be repaired; a crosswalk
        # row for an exam that failed costs nothing.
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        seen = []

        def recording_download(self, **kwargs):
            seen.append((tmp_path / "out_crosswalk.csv").exists())
            kwargs["output"].write_bytes(b"zip-bytes")

        monkeypatch.setattr(StubClient, "download", recording_download)
        download_cohort(matched_csv=csv_path, output=out)
        assert seen == [True, True]

    def test_the_client_is_still_called_with_the_real_identifiers(
        self, tmp_path, stub_client
    ):
        # The pseudonyms are for the filesystem; the API needs the real ones.
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        download_cohort(matched_csv=csv_path, output=tmp_path / "out")

        calls = stub_client.instances[0].calls
        assert [c["accession"] for c in calls] == ["U1", "C1"]
        assert all(c["mrn"] == "A1" for c in calls)

    def test_resuming_reuses_the_same_ids(self, tmp_path, stub_client):
        rows = [
            ["A1", "U1", "2021-03-02", "C1"],
            ["A2", "U2", "2021-03-05", "C2"],
        ]
        csv_path = write_csv(tmp_path / "m.csv", rows)
        out = tmp_path / "out"

        download_cohort(matched_csv=csv_path, output=out, n=1)
        first_run = {p.relative_to(out) for p in out.rglob("*.zip")}
        download_cohort(matched_csv=csv_path, output=out)
        after = {p.relative_to(out) for p in out.rglob("*.zip")}

        # Every path the verification run created still exists, so the second
        # run continued it rather than filing the same visit again.
        assert first_run <= after
        assert len(first_run) == 2 and len(after) == 4

    def test_a_resumed_run_skips_what_is_already_there(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        download_cohort(matched_csv=csv_path, output=out)
        download_cohort(matched_csv=csv_path, output=out)

        assert len(stub_client.instances[0].calls) == 2
        assert stub_client.instances[1].calls == []

    def test_a_patient_added_later_does_not_renumber_the_first(
        self, tmp_path, stub_client
    ):
        out = tmp_path / "out"
        first_csv = write_csv(
            tmp_path / "one.csv", [["A1", "U1", "2021-03-02", "C1"]]
        )
        download_cohort(matched_csv=first_csv, output=out)

        second_csv = write_csv(
            tmp_path / "two.csv",
            [
                ["A2", "U2", "2021-01-01", "C2"],
                ["A1", "U1", "2021-03-02", "C1"],
            ],
        )
        download_cohort(matched_csv=second_csv, output=out)

        # A1 keeps P0001 even though A2's visit is earlier and its row is first.
        assert (out / "P0001" / "visit-01" / "us" / "A0001.zip").exists()
        assert (out / "P0002" / "visit-01").is_dir()
        assert sorted(p.name for p in out.iterdir()) == ["P0001", "P0002"]

    def test_a_second_visit_for_a_known_patient_appends(self, tmp_path, stub_client):
        out = tmp_path / "out"
        download_cohort(
            matched_csv=write_csv(
                tmp_path / "one.csv", [["A1", "U1", "2021-03-02", "C1"]]
            ),
            output=out,
        )
        download_cohort(
            matched_csv=write_csv(
                tmp_path / "two.csv",
                [
                    ["A1", "U1", "2021-03-02", "C1"],
                    ["A1", "U2", "2021-04-02", "C2"],
                ],
            ),
            output=out,
        )
        assert sorted(p.name for p in (out / "P0001").iterdir()) == [
            "visit-01",
            "visit-02",
        ]

    def test_dry_run_writes_no_crosswalk(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        download_cohort(matched_csv=csv_path, output=tmp_path / "out", dry_run=True)
        assert not (tmp_path / "out_crosswalk.csv").exists()

    def test_a_tree_from_before_pseudonymization_is_flagged(
        self, tmp_path, stub_client, caplog
    ):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        out = tmp_path / "out"
        (out / "00123456" / "03-02-21").mkdir(parents=True)

        with caplog.at_level("WARNING"):
            download_cohort(matched_csv=csv_path, output=out)

        assert "not named for an anonymous patient ID" in caplog.text
        assert "00123456" not in caplog.text


class TestResumingFromTheConvertedTree:
    """Once an archive is deleted, its converted array is the only evidence.

    Batched ingest converts and then deletes each archive, so resume cannot
    key on the ``.zip``: it is gone by design. Without this the next run would
    re-download the whole cohort.
    """

    def _converted(self, arrays: Path, *, us: bool = True, ct: bool = True) -> None:
        """Stand in for what air_convert would have written."""
        visit = arrays / "P0001" / "visit-01"
        if us:
            (visit / "us" / "A0001.zarr").mkdir(parents=True, exist_ok=True)
        if ct:
            (visit / "ct").mkdir(parents=True, exist_ok=True)
            (visit / "ct" / "A0002.nii.gz").write_bytes(b"nifti")

    def test_a_converted_exam_is_not_downloaded_again(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        arrays = tmp_path / "arrays"
        self._converted(arrays)

        download_cohort(
            matched_csv=csv_path,
            output=tmp_path / "out",
            converted_dir=arrays,
        )
        assert stub_client.instances[0].calls == []

    def test_the_archive_being_gone_does_not_trigger_a_download(
        self, tmp_path, stub_client
    ):
        # The whole point: no .zip on disk, and still nothing is fetched.
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        arrays = tmp_path / "arrays"
        self._converted(arrays)
        out = tmp_path / "out"

        download_cohort(matched_csv=csv_path, output=out, converted_dir=arrays)
        assert not (out / "P0001" / "visit-01" / "us" / "A0001.zip").exists()
        assert stub_client.instances[0].calls == []

    def test_only_the_converted_half_is_skipped(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        arrays = tmp_path / "arrays"
        self._converted(arrays, us=True, ct=False)

        download_cohort(
            matched_csv=csv_path,
            output=tmp_path / "out",
            converted_dir=arrays,
        )
        calls = stub_client.instances[0].calls
        assert [c["accession"] for c in calls] == ["C1"]

    def test_without_the_flag_nothing_changes(self, tmp_path, stub_client):
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        self._converted(tmp_path / "arrays")

        download_cohort(matched_csv=csv_path, output=tmp_path / "out")
        assert len(stub_client.instances[0].calls) == 2

    def test_skip_existing_off_re_downloads_a_converted_exam(
        self, tmp_path, stub_client
    ):
        # --skip_existing is the one switch that means "fetch it anyway".
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        arrays = tmp_path / "arrays"
        self._converted(arrays)

        download_cohort(
            matched_csv=csv_path,
            output=tmp_path / "out",
            converted_dir=arrays,
            skip_existing=False,
        )
        assert len(stub_client.instances[0].calls) == 2

    def test_identifiers_are_still_assigned_for_a_skipped_exam(
        self, tmp_path, stub_client
    ):
        # Skipping the download must not skip the crosswalk, or a resumed
        # run would renumber the cohort.
        csv_path = write_csv(tmp_path / "m.csv", [["A1", "U1", "2021-03-02", "C1"]])
        arrays = tmp_path / "arrays"
        self._converted(arrays)
        cw = tmp_path / "cw.csv"

        download_cohort(
            matched_csv=csv_path,
            output=tmp_path / "out",
            crosswalk_csv=cw,
            converted_dir=arrays,
        )
        rows = list(csv.DictReader(cw.open()))
        assert {r["anon_accession_number"] for r in rows} == {"A0001", "A0002"}


class TestNoIdentifiersReachTheOutputTree:
    """The strongest guarantee here: grep the whole tree and find nothing."""

    def test_no_identifier_appears_in_any_path_under_the_output(
        self, tmp_path, stub_client
    ):
        csv_path = write_csv(
            tmp_path / "m.csv",
            [
                ["MRN-SECRET", "US-SECRET", "2021-03-02T06:00:00-08:00", "CT-SECRET"],
                ["MRN-TWO", "US-TWO", "2021-03-05T06:00:00-08:00", "CT-TWO"],
            ],
        )
        out = tmp_path / "out"
        download_cohort(matched_csv=csv_path, output=out)

        every_path = " ".join(str(p) for p in out.rglob("*"))
        for secret in ("MRN-SECRET", "US-SECRET", "CT-SECRET", "MRN-TWO", "03-02-21"):
            assert secret not in every_path

    def test_identifiers_are_not_logged(self, tmp_path, stub_client, caplog):
        csv_path = write_csv(
            tmp_path / "m.csv",
            [["MRN-SECRET", "US-SECRET", "2021-03-02T06:00:00-08:00", "CT-SECRET"]],
        )
        with caplog.at_level("DEBUG"):
            download_cohort(matched_csv=csv_path, output=tmp_path / "out")

        for secret in ("MRN-SECRET", "US-SECRET", "CT-SECRET"):
            assert secret not in caplog.text


class TestLeadingZeroMrn:
    """A leading zero identifies a patient, so it must survive to the crosswalk.

    It no longer reaches a folder name -- nothing real does -- so what these
    pin is that it arrives intact at the one file that still holds it.
    """

    def test_the_crosswalk_keeps_the_leading_zero(self, tmp_path, stub_client):
        csv_path = write_csv(
            tmp_path / "m.csv",
            [["00123456", "0099", "2021-03-02T06:00:00-08:00", "0100"]],
        )
        download_cohort(matched_csv=csv_path, output=tmp_path / "out")
        assert "00123456,0099" in (tmp_path / "out_crosswalk.csv").read_text()

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
        shared = crosswalk(tmp_path)
        first, _ = build_visit_paths(tmp_path / "out", row(mrn="0123"), shared)
        second, _ = build_visit_paths(tmp_path / "out", row(mrn="123"), shared)
        assert first.parts[-4] == "P0001"
        assert second.parts[-4] == "P0002"
        assert first != second
