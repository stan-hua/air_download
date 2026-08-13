"""Tests for matching ultrasound exams to the CTs that followed them.

Every fixture uses synthetic identifiers written to tmp_path; no real
search output is ever read.
"""

import csv

import pytest

from air_download.us_ct.match import (
    MATCH_CSV_HEADER,
    count_ambiguous_cts,
    match_exams,
    read_exams,
    select_one_us_per_ct,
    write_matches_csv,
)

HEADER = "mrn,accession_number,date_time,sex,birthdate,description,image_count"


def write_csv(tmp_path, name, *rows, header=HEADER):
    """Write a synthetic search-result CSV and return its path."""
    path = tmp_path / name
    path.write_text("\n".join([header, *rows]) + "\n")
    return path


def exam(mrn, accession, when, description="EXAM", image_count=None):
    """Build a parsed exam the way read_exams would.

    ``image_count`` is left out entirely when None, mirroring an older
    accessions.csv written before the column existed.
    """
    from air_download.utils import parse_datetime

    built = {
        "mrn": mrn,
        "accession_number": accession,
        "date_time": when,
        "description": description,
        "when": parse_datetime(when),
    }
    if image_count is not None:
        built["image_count"] = image_count
    return built


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


class TestMultiplePrecedingUltrasounds:
    """Tests for flagging a CT preceded by more than one ultrasound."""

    @pytest.fixture
    def two_us_one_ct(self):
        """Two ultrasounds, then one CT, all inside the window."""
        us = [
            exam("A1", "U_EARLY", "2021-03-02T06:00:00-08:00"),
            exam("A1", "U_LATE", "2021-03-02T09:00:00-08:00"),
        ]
        ct = [exam("A1", "C1", "2021-03-02T10:00:00-08:00")]
        return match_exams(us, ct)

    def test_both_ultrasounds_produce_rows(self, two_us_one_ct):
        assert [m["us_accession_number"] for m in two_us_one_ct] == [
            "U_EARLY",
            "U_LATE",
        ]

    def test_count_reported_on_every_row(self, two_us_one_ct):
        assert [m["n_preceding_us"] for m in two_us_one_ct] == [2, 2]

    def test_rank_is_chronological(self, two_us_one_ct):
        assert [m["us_rank_before_ct"] for m in two_us_one_ct] == [1, 2]

    def test_closest_flag_marks_the_last_ultrasound(self, two_us_one_ct):
        assert [m["is_closest_us"] for m in two_us_one_ct] == [False, True]

    def test_single_ultrasound_is_closest_and_unambiguous(self):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [exam("A1", "C1", "2021-03-02T10:00:00-08:00")]
        row = match_exams(us, ct)[0]
        assert (row["n_preceding_us"], row["us_rank_before_ct"]) == (1, 1)
        assert row["is_closest_us"] is True

    def test_ultrasound_outside_window_not_counted(self):
        # U_OLD is 30h before the CT, so it does not make the CT ambiguous.
        us = [
            exam("A1", "U_OLD", "2021-03-01T04:00:00-08:00"),
            exam("A1", "U_NEW", "2021-03-02T09:00:00-08:00"),
        ]
        ct = [exam("A1", "C1", "2021-03-02T10:00:00-08:00")]
        matches = match_exams(us, ct)
        assert [m["n_preceding_us"] for m in matches] == [1]
        assert matches[0]["us_accession_number"] == "U_NEW"

    def test_counts_ultrasound_paired_to_a_different_ct(self):
        # U1 pairs with C_A, U2 with C_B, but both precede C_B in the window,
        # so C_B's row must still report the ambiguity.
        us = [
            exam("A1", "U1", "2021-03-02T08:00:00-08:00"),
            exam("A1", "U2", "2021-03-02T12:00:00-08:00"),
        ]
        ct = [
            exam("A1", "C_A", "2021-03-02T10:00:00-08:00"),
            exam("A1", "C_B", "2021-03-02T20:00:00-08:00"),
        ]
        matches = match_exams(us, ct)
        by_ct = {m["ct_accession_number"]: m for m in matches}
        assert by_ct["C_A"]["n_preceding_us"] == 1
        assert by_ct["C_B"]["n_preceding_us"] == 2

    def test_three_ultrasounds_rank_in_order(self):
        us = [
            exam("A1", "U1", "2021-03-02T05:00:00-08:00"),
            exam("A1", "U2", "2021-03-02T06:00:00-08:00"),
            exam("A1", "U3", "2021-03-02T07:00:00-08:00"),
        ]
        ct = [exam("A1", "C1", "2021-03-02T08:00:00-08:00")]
        matches = match_exams(us, ct)
        assert [m["us_rank_before_ct"] for m in matches] == [1, 2, 3]
        assert [m["is_closest_us"] for m in matches] == [False, False, True]

    def test_identical_timestamps_do_not_confuse_ranking(self):
        # Two ultrasounds at the same instant: distinct rows, distinct ranks.
        us = [
            exam("A1", "U1", "2021-03-02T08:00:00-08:00"),
            exam("A1", "U2", "2021-03-02T08:00:00-08:00"),
        ]
        ct = [exam("A1", "C1", "2021-03-02T10:00:00-08:00")]
        matches = match_exams(us, ct)
        assert sorted(m["us_rank_before_ct"] for m in matches) == [1, 2]
        assert sum(m["is_closest_us"] for m in matches) == 1

    def test_other_patients_not_counted(self):
        us = [
            exam("A1", "U1", "2021-03-02T08:00:00-08:00"),
            exam("A2", "U2", "2021-03-02T09:00:00-08:00"),
        ]
        ct = [exam("A1", "C1", "2021-03-02T10:00:00-08:00")]
        matches = match_exams(us, ct)
        assert [m["n_preceding_us"] for m in matches] == [1]


class TestCountAmbiguousCts:
    """Tests for the ambiguity summary."""

    def test_counts_distinct_cts(self):
        us = [
            exam("A1", "U1", "2021-03-02T06:00:00-08:00"),
            exam("A1", "U2", "2021-03-02T09:00:00-08:00"),
        ]
        ct = [exam("A1", "C1", "2021-03-02T10:00:00-08:00")]
        assert count_ambiguous_cts(match_exams(us, ct)) == 1

    def test_zero_when_unambiguous(self):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00")]
        ct = [exam("A1", "C1", "2021-03-02T10:00:00-08:00")]
        assert count_ambiguous_cts(match_exams(us, ct)) == 0

    def test_same_accession_different_patients_counted_separately(self):
        us = [
            exam("A1", "U1", "2021-03-02T06:00:00-08:00"),
            exam("A1", "U2", "2021-03-02T09:00:00-08:00"),
            exam("A2", "U3", "2021-06-02T06:00:00-08:00"),
            exam("A2", "U4", "2021-06-02T09:00:00-08:00"),
        ]
        ct = [
            exam("A1", "C1", "2021-03-02T10:00:00-08:00"),
            exam("A2", "C1", "2021-06-02T10:00:00-08:00"),
        ]
        assert count_ambiguous_cts(match_exams(us, ct)) == 2

    def test_empty(self):
        assert count_ambiguous_cts([]) == 0


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


class TestImageCounts:
    """The image counts carried through from the search results."""

    def test_counts_are_carried_through(self):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00", image_count="14")]
        ct = [exam("A1", "C1", "2021-03-02T14:00:00-08:00", image_count="480")]
        (row,) = match_exams(us, ct)
        assert row["us_image_count"] == "14"
        assert row["ct_image_count"] == "480"

    def test_csv_without_the_column_still_matches(self, tmp_path):
        # An accessions.csv predating image_count must keep working.
        header = "mrn,accession_number,date_time,sex,birthdate,description"
        us_csv = write_csv(
            tmp_path, "us.csv", "A1,U1,2021-03-02T08:00:00-08:00,,,US", header=header
        )
        ct_csv = write_csv(
            tmp_path, "ct.csv", "A1,C1,2021-03-02T14:00:00-08:00,,,CT", header=header
        )
        (row,) = match_exams(read_exams(us_csv), read_exams(ct_csv))
        assert row["us_image_count"] == ""
        assert row["ct_image_count"] == ""


class TestSelectOneUsPerCt:
    """Narrowing a CT preceded by several ultrasounds down to one."""

    @staticmethod
    def _two_us_one_ct():
        """U1 is earlier but larger; U2 is nearer the CT but smaller."""
        us = [
            exam("A1", "U1", "2021-03-02T08:00:00-08:00", image_count="14"),
            exam("A1", "U2", "2021-03-02T10:00:00-08:00", image_count="3"),
        ]
        ct = [exam("A1", "C1", "2021-03-02T14:00:00-08:00", image_count="480")]
        return match_exams(us, ct)

    def test_all_is_a_passthrough(self):
        matches = self._two_us_one_ct()
        kept, without = select_one_us_per_ct(matches, "all")
        assert kept is matches
        assert without == 0

    def test_closest_keeps_the_later_ultrasound(self):
        kept, without = select_one_us_per_ct(self._two_us_one_ct(), "closest")
        assert [r["us_accession_number"] for r in kept] == ["U2"]
        assert without == 0

    def test_most_images_keeps_the_larger_ultrasound(self):
        kept, without = select_one_us_per_ct(self._two_us_one_ct(), "most_images")
        assert [r["us_accession_number"] for r in kept] == ["U1"]
        assert without == 0

    def test_most_images_falls_back_to_closest_without_counts(self):
        us = [
            exam("A1", "U1", "2021-03-02T08:00:00-08:00"),
            exam("A1", "U2", "2021-03-02T10:00:00-08:00"),
        ]
        ct = [exam("A1", "C1", "2021-03-02T14:00:00-08:00")]
        kept, without = select_one_us_per_ct(match_exams(us, ct), "most_images")
        assert [r["us_accession_number"] for r in kept] == ["U2"]
        assert without == 1

    def test_most_images_ignores_a_row_with_no_count(self):
        us = [
            exam("A1", "U1", "2021-03-02T08:00:00-08:00", image_count="14"),
            exam("A1", "U2", "2021-03-02T10:00:00-08:00"),
        ]
        ct = [exam("A1", "C1", "2021-03-02T14:00:00-08:00")]
        kept, _ = select_one_us_per_ct(match_exams(us, ct), "most_images")
        assert [r["us_accession_number"] for r in kept] == ["U1"]

    @pytest.mark.parametrize("strategy", ["closest", "most_images"])
    @pytest.mark.parametrize("all_pairs", [False, True])
    def test_no_ct_is_dropped(self, strategy, all_pairs):
        # Ranking within the group, rather than filtering on is_closest_us,
        # is what guarantees every emitted CT keeps exactly one row.
        us = [
            exam("A1", "U1", "2021-03-02T08:00:00-08:00", image_count="14"),
            exam("A1", "U2", "2021-03-02T10:00:00-08:00", image_count="3"),
            exam("A1", "U3", "2021-03-02T12:00:00-08:00"),
        ]
        ct = [
            exam("A1", "C1", "2021-03-02T11:00:00-08:00"),
            exam("A1", "C2", "2021-03-02T14:00:00-08:00"),
        ]
        matches = match_exams(us, ct, all_pairs=all_pairs)
        kept, _ = select_one_us_per_ct(matches, strategy)
        before = {m["ct_accession_number"] for m in matches}
        assert {r["ct_accession_number"] for r in kept} == before
        assert len(kept) == len(before)

    def test_one_row_survives_per_ct(self):
        kept, _ = select_one_us_per_ct(self._two_us_one_ct(), "closest")
        assert len(kept) == 1

    def test_untouched_when_nothing_is_contested(self):
        us = [exam("A1", "U1", "2021-03-02T08:00:00-08:00", image_count="14")]
        ct = [exam("A1", "C1", "2021-03-02T14:00:00-08:00")]
        matches = match_exams(us, ct)
        kept, _ = select_one_us_per_ct(matches, "most_images")
        assert kept == matches

    def test_patients_are_kept_apart(self):
        # The same CT accession under two MRNs must not collapse into one.
        us = [
            exam("A1", "U1", "2021-03-02T08:00:00-08:00", image_count="14"),
            exam("A2", "U2", "2021-03-02T08:00:00-08:00", image_count="9"),
        ]
        ct = [
            exam("A1", "C1", "2021-03-02T14:00:00-08:00"),
            exam("A2", "C1", "2021-03-02T14:00:00-08:00"),
        ]
        kept, _ = select_one_us_per_ct(match_exams(us, ct), "most_images")
        assert len(kept) == 2

    def test_preserves_original_row_order(self):
        us = [
            exam("A1", "U1", "2021-03-02T08:00:00-08:00", image_count="14"),
            exam("A1", "U2", "2021-03-02T10:00:00-08:00", image_count="3"),
            exam("A2", "U3", "2021-03-02T08:00:00-08:00", image_count="7"),
        ]
        ct = [
            exam("A1", "C1", "2021-03-02T14:00:00-08:00"),
            exam("A2", "C2", "2021-03-02T14:00:00-08:00"),
        ]
        kept, _ = select_one_us_per_ct(match_exams(us, ct), "most_images")
        assert [r["us_accession_number"] for r in kept] == ["U1", "U3"]

    def test_rejects_an_unknown_strategy(self):
        with pytest.raises(ValueError, match="Unknown us_selection"):
            select_one_us_per_ct([], "latest")


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
