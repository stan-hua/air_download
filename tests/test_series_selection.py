"""Tests for selecting the thinnest axial series."""

import pytest

from air_download.filters import (
    DEFAULT_AXIAL_PATTERNS,
    is_axial,
    parse_slice_thickness,
    keep_thinnest_axial,
    select_thinnest_axial,
)

PATTERNS = [p.strip() for p in DEFAULT_AXIAL_PATTERNS.split(",")]


def make_series(description, modality="CT", image_count=100):
    """Build a series dictionary shaped like the API's."""
    return {
        "description": description,
        "modality": modality,
        "imageCount": image_count,
    }


class TestParseSliceThickness:
    """Tests for parse_slice_thickness."""

    @pytest.mark.parametrize(
        "description,expected",
        [
            ("AXIAL 0.625MM STD", 0.625),
            ("AX 1.25 MM SOFT", 1.25),
            ("axial 5mm", 5.0),
            ("AXIAL 2.5MM/BONE", 2.5),
            ("CT 10 mm HELICAL", 10.0),
        ],
    )
    def test_parses_stated_thickness(self, description, expected):
        assert parse_slice_thickness(description) == expected

    def test_returns_none_without_thickness(self):
        assert parse_slice_thickness("AXIAL SOFT TISSUE") is None

    def test_returns_none_for_empty(self):
        assert parse_slice_thickness("") is None
        assert parse_slice_thickness(None) is None

    def test_smallest_plausible_value_wins(self):
        assert parse_slice_thickness("AX 5MM RECON 1.25MM") == 1.25

    def test_implausibly_large_value_ignored(self):
        # A 512mm field of view is not a slice thickness.
        assert parse_slice_thickness("AXIAL FOV 512MM") is None

    def test_implausibly_small_value_ignored(self):
        assert parse_slice_thickness("AXIAL 0.01MM") is None

    def test_mixed_plausible_and_implausible(self):
        assert parse_slice_thickness("AX FOV 512MM SLICE 2MM") == 2.0

    def test_digits_without_mm_ignored(self):
        assert parse_slice_thickness("AXIAL SERIES 3") is None


class TestIsAxial:
    """Tests for is_axial."""

    @pytest.mark.parametrize(
        "description",
        ["AXIAL 1.25MM", "AX SOFT TISSUE", "Axial Bone", "TRA 2MM", "TRANSVERSE"],
    )
    def test_matches_axial_names(self, description):
        assert is_axial(make_series(description), PATTERNS)

    @pytest.mark.parametrize(
        "description", ["CORONAL 3MM", "SAG MPR", "SCOUT", "TOPOGRAM"]
    )
    def test_rejects_other_planes(self, description):
        assert not is_axial(make_series(description), PATTERNS)

    def test_whole_word_matching_avoids_false_positives(self):
        # "ax" inside THORAX and "tra" inside TRAUMA must not count.
        assert not is_axial(make_series("THORAX SCOUT"), PATTERNS)
        assert not is_axial(make_series("TRAUMA CORONAL"), PATTERNS)

    def test_missing_description_is_not_axial(self):
        assert not is_axial({"modality": "CT"}, PATTERNS)


class TestSelectThinnestAxial:
    """Tests for select_thinnest_axial."""

    def test_picks_thinnest_stated_thickness(self):
        series = [
            make_series("AXIAL 5MM", image_count=60),
            make_series("AXIAL 0.625MM", image_count=480),
            make_series("AXIAL 2.5MM", image_count=120),
        ]
        assert select_thinnest_axial(series, PATTERNS)["description"] == "AXIAL 0.625MM"

    def test_thickness_beats_image_count(self):
        # The thinnest series is not always the one with the most images.
        series = [
            make_series("AXIAL 1MM", image_count=50),
            make_series("AXIAL 5MM", image_count=900),
        ]
        assert select_thinnest_axial(series, PATTERNS)["description"] == "AXIAL 1MM"

    def test_ties_broken_by_image_count(self):
        series = [
            make_series("AXIAL 1.25MM SOFT", image_count=100),
            make_series("AXIAL 1.25MM BONE", image_count=400),
        ]
        chosen = select_thinnest_axial(series, PATTERNS)
        assert chosen["imageCount"] == 400

    def test_falls_back_to_image_count(self):
        series = [
            make_series("AXIAL SOFT", image_count=120),
            make_series("AXIAL BONE", image_count=480),
        ]
        assert select_thinnest_axial(series, PATTERNS)["imageCount"] == 480

    def test_partial_thickness_info_prefers_measured(self):
        # A stated thickness wins even if another series has more images.
        series = [
            make_series("AXIAL SOFT", image_count=900),
            make_series("AXIAL 2MM BONE", image_count=100),
        ]
        assert select_thinnest_axial(series, PATTERNS)["description"] == "AXIAL 2MM BONE"

    def test_ignores_non_axial_series(self):
        series = [
            make_series("CORONAL 0.5MM", image_count=900),
            make_series("AXIAL 3MM", image_count=100),
        ]
        assert select_thinnest_axial(series, PATTERNS)["description"] == "AXIAL 3MM"

    def test_ignores_non_ct_modalities(self):
        series = [make_series("AXIAL 1MM", modality="MR", image_count=500)]
        assert select_thinnest_axial(series, PATTERNS) is None

    def test_missing_modality_is_treated_as_candidate(self):
        series = [{"description": "AXIAL 1MM", "imageCount": 300}]
        assert select_thinnest_axial(series, PATTERNS) is not None

    def test_returns_none_without_axial_series(self):
        series = [make_series("CORONAL 3MM"), make_series("SCOUT")]
        assert select_thinnest_axial(series, PATTERNS) is None

    def test_handles_missing_image_count(self):
        series = [make_series("AXIAL SOFT"), {"description": "AXIAL BONE"}]
        assert select_thinnest_axial(series, PATTERNS) is not None


class TestKeepThinnestAxial:
    """Tests for keep_thinnest_axial."""

    @pytest.fixture
    def study(self):
        """A CT study with scouts, several axials, reformats, and a report."""
        return [
            make_series("SCOUT", image_count=2),
            make_series("AXIAL 5MM STD", image_count=60),
            make_series("AXIAL 0.625MM BONE", image_count=480),
            make_series("AXIAL 2.5MM SOFT", image_count=120),
            make_series("COR 3MM MPR", image_count=90),
            make_series("Dose Report", modality="SR", image_count=1),
        ]

    def test_keeps_the_thinnest_axial_alone(self, study):
        selected = keep_thinnest_axial(study)
        assert [s["description"] for s in selected] == ["AXIAL 0.625MM BONE"]

    def test_drops_structured_reports(self, study):
        # The report carries no image data, so it is not wanted.
        selected = keep_thinnest_axial(study)
        assert all(s.get("modality") != "SR" for s in selected)

    def test_drops_every_structured_report(self):
        series = [
            make_series("AXIAL 1MM", image_count=300),
            make_series("Dose Report", modality="SR", image_count=1),
            make_series("Radiation Dose", modality="SR", image_count=1),
        ]
        selected = keep_thinnest_axial(series)
        assert [s["description"] for s in selected] == ["AXIAL 1MM"]

    def test_without_axial_keeps_nothing(self, caplog):
        series = [
            make_series("CORONAL 3MM", image_count=90),
            make_series("Dose Report", modality="SR", image_count=1),
        ]
        with caplog.at_level("WARNING"):
            selected = keep_thinnest_axial(series)
        assert selected == []
        assert "no axial ct series" in caplog.text.lower()
        # The consequence has to be spelled out: the exam yields no archive.
        assert "no archive is written" in caplog.text.lower()

    def test_without_report_keeps_axial_only(self):
        series = [
            make_series("SCOUT", image_count=2),
            make_series("AXIAL 1MM", image_count=300),
        ]
        selected = keep_thinnest_axial(series)
        assert [s["description"] for s in selected] == ["AXIAL 1MM"]

    def test_empty_input_returns_empty(self):
        assert keep_thinnest_axial([]) == []

    def test_custom_patterns_are_honoured(self):
        series = [
            make_series("TRANSVERSE 1MM", image_count=300),
            make_series("AXIAL 2MM", image_count=300),
        ]
        selected = keep_thinnest_axial(series, axial_patterns="transverse")
        assert [s["description"] for s in selected] == ["TRANSVERSE 1MM"]

    def test_never_returns_more_than_one_series(self):
        series = [
            make_series("AXIAL 1MM A", image_count=300),
            make_series("AXIAL 1MM B", image_count=300),
        ]
        assert len(keep_thinnest_axial(series)) == 1

    def test_returns_the_series_object_itself(self, study):
        # Identity matters: probe marks rows by matching on the same object.
        (selected,) = keep_thinnest_axial(study)
        assert any(s is selected for s in study)
