"""Tests for listing an exam's series without downloading it.

All identifiers here are synthetic, and the AIR client is replaced with a
stub, so no test touches the network or a real accession number.
"""

# Standard libraries
import csv
from pathlib import Path

# Non-standard libraries
import pytest

# Custom libraries
from air_download import probe
from air_download.probe import (
    PER_SERIES_HEADER,
    SUMMARY_HEADER,
    probe_series,
    read_exam_pairs,
    write_probe_csv,
)

MATCHED_HEADER = ["mrn", "us_accession_number", "us_date_time", "ct_accession_number"]
SEARCH_HEADER = [
    "mrn",
    "accession_number",
    "date_time",
    "sex",
    "birthdate",
    "description",
    "image_count",
]


def write_csv(path: Path, rows: list[list[str]], header: list[str]) -> Path:
    """Write a synthetic CSV and return its path."""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return path


class StubClient:
    """Returns canned exams and series, and records what was asked for."""

    instances: list["StubClient"] = []

    # Series keyed by accession number; anything absent gets one series.
    series_by_accession: dict[str, list[dict]] = {}
    # Accessions the search should find nothing for.
    missing: set[str] = set()

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.searches: list[dict] = []
        self.series_calls: list[dict] = []
        self.downloads: list[dict] = []
        StubClient.instances.append(self)

    def search(self, **kwargs):
        self.searches.append(kwargs)
        accession = kwargs["accession"]
        if accession in StubClient.missing:
            return []
        return [
            {
                "accessionNumber": accession,
                "dateTime": "2021-03-02T08:00:00-08:00",
                "description": "US ED BEDSIDE",
                "imageCount": 12,
                "studyUid": f"1.2.{accession}",
            }
        ]

    def list_series(self, study):
        self.series_calls.append(study)
        return StubClient.series_by_accession.get(
            study["accessionNumber"],
            [{"description": "VIEW", "imageCount": 4, "modality": "US",
              "seriesNumber": "001", "seriesUid": "9.9.1"}],
        )

    def download(self, **kwargs):  # pragma: no cover - must never be called
        self.downloads.append(kwargs)
        raise AssertionError("probing must not download")


@pytest.fixture
def stub_client(monkeypatch):
    """Replace AIRClient in the probe module with the recording stub."""
    StubClient.instances = []
    StubClient.series_by_accession = {}
    StubClient.missing = set()
    monkeypatch.setattr(probe, "AIRClient", StubClient)
    yield StubClient


def read_rows(path: Path) -> list[dict]:
    """Read a written CSV back."""
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class TestReadExamPairs:
    """Choosing exams from either CSV this package writes."""

    def test_matched_csv_defaults_to_ultrasound(self, tmp_path):
        path = write_csv(
            tmp_path / "matched.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"]],
            MATCHED_HEADER,
        )
        assert read_exam_pairs(path) == [("A1", "U1")]

    def test_matched_csv_ct(self, tmp_path):
        path = write_csv(
            tmp_path / "matched.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"]],
            MATCHED_HEADER,
        )
        assert read_exam_pairs(path, "ct") == [("A1", "C1")]

    def test_matched_csv_both(self, tmp_path):
        path = write_csv(
            tmp_path / "matched.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"]],
            MATCHED_HEADER,
        )
        assert read_exam_pairs(path, "both") == [("A1", "U1"), ("A1", "C1")]

    def test_repeated_ct_is_collapsed(self, tmp_path):
        # Two ultrasounds before one CT: the CT must be probed once.
        path = write_csv(
            tmp_path / "matched.csv",
            [
                ["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"],
                ["A1", "U2", "2021-03-02T10:00:00-08:00", "C1"],
            ],
            MATCHED_HEADER,
        )
        assert read_exam_pairs(path, "ct") == [("A1", "C1")]
        assert read_exam_pairs(path, "both") == [
            ("A1", "U1"),
            ("A1", "C1"),
            ("A1", "U2"),
        ]

    def test_search_csv_is_detected(self, tmp_path):
        path = write_csv(
            tmp_path / "accessions.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", "", "", "US", "12"]],
            SEARCH_HEADER,
        )
        assert read_exam_pairs(path) == [("A1", "U1")]

    def test_same_accession_under_two_mrns_is_kept(self, tmp_path):
        # An accession number is only unique within a patient.
        path = write_csv(
            tmp_path / "matched.csv",
            [
                ["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"],
                ["A2", "U1", "2021-03-02T08:00:00-08:00", "C2"],
            ],
            MATCHED_HEADER,
        )
        assert read_exam_pairs(path) == [("A1", "U1"), ("A2", "U1")]

    def test_rejects_unknown_which(self, tmp_path):
        path = write_csv(tmp_path / "matched.csv", [], MATCHED_HEADER)
        with pytest.raises(ValueError, match="Unknown which"):
            read_exam_pairs(path, "everything")


class TestWriteProbeCsv:
    """The output file."""

    def test_per_series_header(self, tmp_path):
        out = write_probe_csv([], tmp_path / "probe.csv")
        assert out.read_text().strip() == ",".join(PER_SERIES_HEADER)

    def test_summary_header(self, tmp_path):
        out = write_probe_csv([], tmp_path / "probe.csv", summary=True)
        assert out.read_text().strip() == ",".join(SUMMARY_HEADER)

    def test_overwrites_rather_than_appends(self, tmp_path):
        rows = [dict.fromkeys(PER_SERIES_HEADER, "x")]
        out = tmp_path / "probe.csv"
        write_probe_csv(rows, out)
        write_probe_csv(rows, out)
        assert len(read_rows(out)) == 1


class TestProbeSeries:
    """End-to-end probing against the stub client."""

    def _matched(self, tmp_path, rows=None):
        return write_csv(
            tmp_path / "matched.csv",
            rows or [["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"]],
            MATCHED_HEADER,
        )

    def test_writes_one_row_per_series(self, tmp_path, stub_client):
        stub_client.series_by_accession = {
            "U1": [
                {"description": "RUQ", "imageCount": 3, "modality": "US",
                 "seriesNumber": "001", "seriesUid": "9.9.1"},
                {"description": "LUQ", "imageCount": 5, "modality": "US",
                 "seriesNumber": "002", "seriesUid": "9.9.2"},
            ]
        }
        out = tmp_path / "probe.csv"
        probe_series(self._matched(tmp_path), output=out)
        rows = read_rows(out)
        assert [r["series_description"] for r in rows] == ["RUQ", "LUQ"]
        assert rows[0]["mrn"] == "A1"
        assert rows[0]["accession_number"] == "U1"
        assert rows[0]["study_image_count"] == "12"
        assert rows[0]["series_image_count"] == "3"

    def test_summary_is_one_row_per_exam(self, tmp_path, stub_client):
        stub_client.series_by_accession = {
            "U1": [
                {"description": "RUQ", "imageCount": 3, "modality": "US"},
                {"description": "LUQ", "imageCount": 5, "modality": "US"},
            ]
        }
        out = tmp_path / "probe.csv"
        probe_series(self._matched(tmp_path), output=out, summary=True)
        (row,) = read_rows(out)
        assert row["n_series"] == "2"
        assert row["total_series_image_count"] == "8"
        assert row["series_descriptions"] == "RUQ | LUQ"

    def test_summary_tolerates_a_missing_image_count(self, tmp_path, stub_client):
        stub_client.series_by_accession = {
            "U1": [{"description": "RUQ"}, {"description": "LUQ", "imageCount": 5}]
        }
        out = tmp_path / "probe.csv"
        probe_series(self._matched(tmp_path), output=out, summary=True)
        (row,) = read_rows(out)
        assert row["total_series_image_count"] == "5"

    def test_never_downloads(self, tmp_path, stub_client):
        probe_series(self._matched(tmp_path), output=tmp_path / "probe.csv")
        client = stub_client.instances[0]
        assert client.downloads == []
        assert len(client.series_calls) == 1

    def test_searches_on_mrn_and_accession_together(self, tmp_path, stub_client):
        probe_series(self._matched(tmp_path), output=tmp_path / "probe.csv")
        (search,) = stub_client.instances[0].searches
        assert search == {"accession": "U1", "mrn": "A1"}

    def test_exam_matching_nothing_is_skipped(self, tmp_path, stub_client):
        stub_client.missing = {"U1"}
        out = tmp_path / "probe.csv"
        probe_series(self._matched(tmp_path), output=out)
        assert read_rows(out) == []

    def test_a_failure_does_not_end_the_run(self, tmp_path, stub_client):
        def explode(self, study):
            if study["accessionNumber"] == "U1":
                raise RuntimeError("boom")
            return [{"description": "RUQ", "imageCount": 1, "modality": "US"}]

        stub_client.list_series = explode
        out = tmp_path / "probe.csv"
        probe_series(
            self._matched(
                tmp_path,
                [
                    ["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"],
                    ["A2", "U2", "2021-03-02T08:00:00-08:00", "C2"],
                ],
            ),
            output=out,
        )
        rows = read_rows(out)
        assert [r["accession_number"] for r in rows] == ["U2"]

    def test_n_caps_the_run(self, tmp_path, stub_client):
        probe_series(
            self._matched(
                tmp_path,
                [
                    ["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"],
                    ["A2", "U2", "2021-03-02T08:00:00-08:00", "C2"],
                ],
            ),
            output=tmp_path / "probe.csv",
            n=1,
        )
        assert len(stub_client.instances[0].searches) == 1

    def test_n_below_one_raises(self, tmp_path, stub_client):
        with pytest.raises(ValueError, match="n must be at least 1"):
            probe_series(self._matched(tmp_path), output=tmp_path / "p.csv", n=0)

    def test_empty_input_makes_no_client(self, tmp_path, stub_client):
        probe_series(
            write_csv(tmp_path / "matched.csv", [], MATCHED_HEADER),
            output=tmp_path / "probe.csv",
        )
        assert stub_client.instances == []
