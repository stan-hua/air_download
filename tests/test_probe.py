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
    matched_modalities,
    probe_csv_header,
    probe_series,
    read_exam_pairs,
    write_probe_csv,
)

# A CT with scouts, several axials, a reformat, and a structured report.
CT_SERIES = [
    {"description": "SCOUT", "imageCount": 2, "modality": "CT", "seriesNumber": "1"},
    {"description": "AXIAL 5MM STD", "imageCount": 60, "modality": "CT",
     "seriesNumber": "2"},
    {"description": "AXIAL 0.625MM BONE", "imageCount": 480, "modality": "CT",
     "seriesNumber": "3"},
    {"description": "COR 3MM MPR", "imageCount": 90, "modality": "CT",
     "seriesNumber": "4"},
    {"description": "Dose Report", "imageCount": 1, "modality": "SR",
     "seriesNumber": "999"},
]

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


class TestMatchedModalities:
    """Reading a pairing's modalities off its header."""

    def test_finds_them_in_header_order(self):
        assert matched_modalities(MATCHED_HEADER) == ["us", "ct"]

    def test_any_modality_not_just_us_and_ct(self):
        header = ["mrn", "mr_accession_number", "xr_accession_number"]
        assert matched_modalities(header) == ["mr", "xr"]

    def test_arbitrary_number_of_modalities(self):
        header = [
            "mrn",
            "us_accession_number",
            "ct_accession_number",
            "mr_accession_number",
            "pet_accession_number",
        ]
        assert matched_modalities(header) == ["us", "ct", "mr", "pet"]

    def test_unprefixed_accession_number_is_not_a_modality(self):
        # This is what tells a search-result CSV apart from a matched one.
        assert matched_modalities(SEARCH_HEADER) == []

    def test_case_is_normalised(self):
        assert matched_modalities(["MRN", "US_accession_number"]) == ["us"]

    def test_repeats_collapse(self):
        header = ["us_accession_number", "us_accession_number"]
        assert matched_modalities(header) == ["us"]


class TestReadExamPairs:
    """Choosing exams from either CSV this package writes."""

    def test_matched_csv_defaults_to_every_modality(self, tmp_path):
        path = write_csv(
            tmp_path / "matched.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"]],
            MATCHED_HEADER,
        )
        assert read_exam_pairs(path) == [("A1", "U1"), ("A1", "C1")]

    def test_one_modality(self, tmp_path):
        path = write_csv(
            tmp_path / "matched.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"]],
            MATCHED_HEADER,
        )
        assert read_exam_pairs(path, "ct") == [("A1", "C1")]
        assert read_exam_pairs(path, "us") == [("A1", "U1")]

    def test_comma_separated_selection(self, tmp_path):
        header = ["mrn", "us_accession_number", "ct_accession_number",
                  "mr_accession_number"]
        path = write_csv(tmp_path / "matched.csv", [["A1", "U1", "C1", "M1"]], header)
        assert read_exam_pairs(path, "us,mr") == [("A1", "U1"), ("A1", "M1")]

    def test_selection_order_is_the_callers(self, tmp_path):
        path = write_csv(
            tmp_path / "matched.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"]],
            MATCHED_HEADER,
        )
        assert read_exam_pairs(path, "ct,us") == [("A1", "C1"), ("A1", "U1")]

    def test_a_list_is_accepted(self, tmp_path):
        path = write_csv(
            tmp_path / "matched.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"]],
            MATCHED_HEADER,
        )
        assert read_exam_pairs(path, ["us"]) == [("A1", "U1")]

    def test_modality_beyond_us_and_ct(self, tmp_path):
        header = ["mrn", "mr_accession_number", "xr_accession_number"]
        path = write_csv(tmp_path / "matched.csv", [["A1", "M1", "X1"]], header)
        assert read_exam_pairs(path) == [("A1", "M1"), ("A1", "X1")]
        assert read_exam_pairs(path, "xr") == [("A1", "X1")]

    def test_repeated_accession_is_collapsed(self, tmp_path):
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
        assert read_exam_pairs(path) == [
            ("A1", "U1"),
            ("A1", "C1"),
            ("A1", "U2"),
        ]

    def test_blank_accession_is_skipped(self, tmp_path):
        # A row pairing only some modalities must not yield an empty lookup.
        path = write_csv(
            tmp_path / "matched.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", ""]],
            MATCHED_HEADER,
        )
        assert read_exam_pairs(path) == [("A1", "U1")]

    def test_row_without_mrn_is_skipped(self, tmp_path):
        path = write_csv(
            tmp_path / "matched.csv",
            [["", "U1", "2021-03-02T08:00:00-08:00", "C1"]],
            MATCHED_HEADER,
        )
        assert read_exam_pairs(path) == []

    def test_search_csv_is_detected(self, tmp_path):
        path = write_csv(
            tmp_path / "accessions.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", "", "", "US", "12"]],
            SEARCH_HEADER,
        )
        assert read_exam_pairs(path) == [("A1", "U1")]

    def test_search_csv_warns_when_modalities_requested(self, tmp_path, caplog):
        path = write_csv(
            tmp_path / "accessions.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", "", "", "US", "12"]],
            SEARCH_HEADER,
        )
        with caplog.at_level("WARNING"):
            assert read_exam_pairs(path, "ct") == [("A1", "U1")]
        assert "pairs no modalities" in caplog.text

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
        assert read_exam_pairs(path, "us") == [("A1", "U1"), ("A2", "U1")]

    def test_rejects_a_modality_the_csv_lacks(self, tmp_path):
        path = write_csv(tmp_path / "matched.csv", [], MATCHED_HEADER)
        with pytest.raises(ValueError, match="no column\\(s\\) for mr"):
            read_exam_pairs(path, "mr")

    def test_rejects_an_empty_selection(self, tmp_path):
        path = write_csv(tmp_path / "matched.csv", [], MATCHED_HEADER)
        with pytest.raises(ValueError, match="No modality requested"):
            read_exam_pairs(path, ",")

    def test_matched_csv_without_mrn_raises(self, tmp_path):
        path = write_csv(
            tmp_path / "matched.csv", [], ["us_accession_number", "ct_accession_number"]
        )
        with pytest.raises(ValueError, match="missing required column: mrn"):
            read_exam_pairs(path)


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
        probe_series(self._matched(tmp_path), output=out, modalities="us")
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
        probe_series(self._matched(tmp_path), output=out, summary=True, modalities="us")
        (row,) = read_rows(out)
        assert row["n_series"] == "2"
        assert row["total_series_image_count"] == "8"
        assert row["series_descriptions"] == "RUQ | LUQ"

    def test_summary_tolerates_a_missing_image_count(self, tmp_path, stub_client):
        stub_client.series_by_accession = {
            "U1": [{"description": "RUQ"}, {"description": "LUQ", "imageCount": 5}]
        }
        out = tmp_path / "probe.csv"
        probe_series(self._matched(tmp_path), output=out, summary=True, modalities="us")
        (row,) = read_rows(out)
        assert row["total_series_image_count"] == "5"

    def test_never_downloads(self, tmp_path, stub_client):
        probe_series(self._matched(tmp_path), output=tmp_path / "probe.csv", modalities="us")
        client = stub_client.instances[0]
        assert client.downloads == []
        assert len(client.series_calls) == 1

    def test_searches_on_mrn_and_accession_together(self, tmp_path, stub_client):
        probe_series(self._matched(tmp_path), output=tmp_path / "probe.csv", modalities="us")
        (search,) = stub_client.instances[0].searches
        assert search == {"accession": "U1", "mrn": "A1"}

    def test_exam_matching_nothing_is_skipped(self, tmp_path, stub_client):
        stub_client.missing = {"U1"}
        out = tmp_path / "probe.csv"
        probe_series(self._matched(tmp_path), output=out, modalities="us")
        assert read_rows(out) == []

    def test_a_failure_does_not_end_the_run(self, tmp_path, stub_client, monkeypatch):
        def explode(self, study):
            if study["accessionNumber"] == "U1":
                raise RuntimeError("boom")
            return [{"description": "RUQ", "imageCount": 1, "modality": "US"}]

        # Through monkeypatch so it is undone: a bare assignment here rebinds
        # the class attribute for the rest of the session.
        monkeypatch.setattr(stub_client, "list_series", explode)
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
            modalities="us",
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
            modalities="us",
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


class TestSelectionPreview:
    """--select marks what --thinnest-axial would keep, dropping nothing."""

    def _matched(self, tmp_path):
        return write_csv(
            tmp_path / "matched.csv",
            [["A1", "U1", "2021-03-02T08:00:00-08:00", "C1"]],
            MATCHED_HEADER,
        )

    def test_header_gains_the_column_only_when_selecting(self):
        assert probe_csv_header() == PER_SERIES_HEADER
        assert probe_csv_header(mark_selection=True) == PER_SERIES_HEADER + ["selected"]
        assert probe_csv_header(summary=True) == SUMMARY_HEADER
        assert probe_csv_header(summary=True, mark_selection=True) == SUMMARY_HEADER + [
            "selected_description",
            "selected_image_count",
        ]

    def test_marks_the_thinnest_axial(self, tmp_path, stub_client):
        stub_client.series_by_accession = {"C1": CT_SERIES}
        out = tmp_path / "probe.csv"
        probe_series(
            self._matched(tmp_path), output=out, modalities="ct",
            select="thinnest_axial",
        )
        rows = read_rows(out)
        selected = [r["series_description"] for r in rows if r["selected"] == "True"]
        assert selected == ["AXIAL 0.625MM BONE"]

    def test_drops_nothing(self, tmp_path, stub_client):
        stub_client.series_by_accession = {"C1": CT_SERIES}
        out = tmp_path / "probe.csv"
        probe_series(
            self._matched(tmp_path), output=out, modalities="ct",
            select="thinnest_axial",
        )
        # Every series is still reported, so you can see what was passed over.
        assert len(read_rows(out)) == len(CT_SERIES)

    def test_structured_report_is_never_selected(self, tmp_path, stub_client):
        stub_client.series_by_accession = {"C1": CT_SERIES}
        out = tmp_path / "probe.csv"
        probe_series(
            self._matched(tmp_path), output=out, modalities="ct",
            select="thinnest_axial",
        )
        (sr,) = [r for r in read_rows(out) if r["series_modality"] == "SR"]
        assert sr["selected"] == "False"

    def test_summary_reports_the_chosen_series(self, tmp_path, stub_client):
        stub_client.series_by_accession = {"C1": CT_SERIES}
        out = tmp_path / "probe.csv"
        probe_series(
            self._matched(tmp_path), output=out, modalities="ct",
            select="thinnest_axial", summary=True,
        )
        (row,) = read_rows(out)
        assert row["selected_description"] == "AXIAL 0.625MM BONE"
        assert row["selected_image_count"] == "480"
        # What exists vs. what would be retrieved: 633 objects, 480 wanted.
        assert row["total_series_image_count"] == "633"

    def test_nothing_selected_for_a_non_ct_exam(self, tmp_path, stub_client):
        stub_client.series_by_accession = {
            "U1": [{"description": "RUQ", "imageCount": 4, "modality": "US"}]
        }
        out = tmp_path / "probe.csv"
        probe_series(
            self._matched(tmp_path), output=out, modalities="us",
            select="thinnest_axial", summary=True,
        )
        (row,) = read_rows(out)
        # Blank, not 0: "no axial series" is not "an axial series with no images".
        assert row["selected_description"] == ""
        assert row["selected_image_count"] == ""

    def test_warns_when_a_probed_exam_selects_nothing(self, tmp_path, stub_client,
                                                      caplog):
        stub_client.series_by_accession = {
            "C1": [{"description": "COR 3MM MPR", "imageCount": 90, "modality": "CT"}]
        }
        out = tmp_path / "probe.csv"
        with caplog.at_level("WARNING"):
            probe_series(
                self._matched(tmp_path), output=out, modalities="ct",
                select="thinnest_axial",
            )
        assert "would download nothing" in caplog.text

    def test_custom_axial_patterns_are_honoured(self, tmp_path, stub_client):
        stub_client.series_by_accession = {
            "C1": [
                {"description": "TRANSVERSE 1MM", "imageCount": 300, "modality": "CT"},
                {"description": "AXIAL 2MM", "imageCount": 300, "modality": "CT"},
            ]
        }
        out = tmp_path / "probe.csv"
        probe_series(
            self._matched(tmp_path), output=out, modalities="ct",
            select="thinnest_axial", axial_patterns="transverse",
        )
        selected = [
            r["series_description"] for r in read_rows(out) if r["selected"] == "True"
        ]
        assert selected == ["TRANSVERSE 1MM"]

    def test_no_selection_columns_without_the_flag(self, tmp_path, stub_client):
        stub_client.series_by_accession = {"C1": CT_SERIES}
        out = tmp_path / "probe.csv"
        probe_series(self._matched(tmp_path), output=out, modalities="ct")
        assert "selected" not in read_rows(out)[0]

    def test_rejects_an_unknown_selection(self, tmp_path, stub_client):
        with pytest.raises(ValueError, match="Unknown select"):
            probe_series(
                self._matched(tmp_path), output=tmp_path / "p.csv", select="biggest",
            )
