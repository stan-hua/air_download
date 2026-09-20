"""Tests for writing a cohort's CT findings keyed by pseudonym.

Every identifier and every report answer here is synthetic. The point of most
of these tests is that nothing real survives the join: the table has to carry
the pseudonyms and the labels, and neither the accession number it joined on
nor the timestamps it read.
"""

# Standard libraries
import csv
import json
from pathlib import Path

# Non-standard libraries
import pytest
import yaml

# Custom libraries
from air_download.crosswalk import CROSSWALK_CSV_HEADER
from air_download.us_ct.labels import (
    QUESTION_PREFIX,
    UNANSWERED,
    build_rows,
    config_digest,
    delta_minutes,
    main,
    parse_answer,
    question_id,
    read_answers,
    read_pairs,
    read_questions,
    write_labels,
)

QUESTIONS = ["Is there ascites?", "Are there any hepatic cysts?"]


@pytest.fixture
def modality_config(tmp_path):
    """A two-question modality config, shaped the way rate's are."""
    path = tmp_path / "abdomen_ct.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "modality": "abdominal_ct",
                "categories": {
                    "Peritoneum": {"questions": [{"question": QUESTIONS[0]}]},
                    "Liver": {"questions": [{"question": QUESTIONS[1]}]},
                },
            }
        )
    )
    return path


@pytest.fixture
def crosswalk(tmp_path):
    """Two complete visits and one with no CT half."""
    path = tmp_path / "cohort_crosswalk.csv"
    rows = [
        ("P0001", "A0001", "visit-01", "us", "a.zip", "111", "US-1", "2026-01-01 08:00:00"),
        ("P0001", "A0002", "visit-01", "ct", "b.zip", "111", "CT-1", "2026-01-01 09:30:00"),
        ("P0002", "A0003", "visit-01", "us", "c.zip", "222", "US-2", "2026-02-02 10:00:00"),
        ("P0002", "A0004", "visit-01", "ct", "d.zip", "222", "CT-2", "2026-02-02 11:00:00"),
        ("P0003", "A0005", "visit-01", "us", "e.zip", "333", "US-3", "2026-03-03 10:00:00"),
    ]
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CROSSWALK_CSV_HEADER)
        writer.writerows(rows)
    return path


@pytest.fixture
def answers(tmp_path):
    """Answers for one of the two CT reports, plus a report nobody downloaded."""
    path = tmp_path / "questions.csv"
    rows = [
        ("CT-1", "Peritoneum", question_id(QUESTIONS[0]), QUESTIONS[0], "Yes"),
        ("CT-1", "Liver", question_id(QUESTIONS[1]), QUESTIONS[1], "No."),
        ("CT-2", "Peritoneum", question_id(QUESTIONS[0]), QUESTIONS[0], "No"),
        ("CT-2", "Liver", question_id(QUESTIONS[1]), QUESTIONS[1], "<think> unclosed"),
        ("CT-9", "Liver", question_id(QUESTIONS[1]), QUESTIONS[1], "Yes"),
    ]
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["report_id", "category", "question_id", "question", "answer"])
        writer.writerows(rows)
    return path


class TestParseAnswer:
    """The verdict comes off free text, the way ifast reads the same field."""

    def test_a_bare_verdict(self):
        assert parse_answer("Yes") == 1
        assert parse_answer("No") == 0

    def test_a_trailing_period(self):
        assert parse_answer("No.") == 0

    def test_reasoning_that_opens_with_a_verdict(self):
        assert parse_answer("No, the report describes no free fluid.") == 0

    def test_the_first_line_that_carries_one_decides(self):
        assert parse_answer("Thinking about it\nYes\nNo") == 1

    def test_no_verdict_at_all(self):
        assert parse_answer("<think> unclosed") == UNANSWERED

    def test_case_does_not_matter(self):
        assert parse_answer("yes") == 1

    def test_a_word_beginning_with_no_is_not_a_verdict(self):
        # "Nodules are present" opens with n-o, and is not a No.
        assert parse_answer("Nodules are present") == UNANSWERED


class TestQuestionId:
    """The hash names the column, so all three copies of it have to agree."""

    def test_it_is_eight_hex_characters(self):
        value = question_id(QUESTIONS[0])
        assert len(value) == 8
        assert set(value) <= set("0123456789abcdef")

    def test_rewording_changes_it(self):
        assert question_id("Is there ascites?") != question_id("Is there any ascites?")


class TestReadPairs:
    """A visit's two halves make a row; one half makes none."""

    def test_it_pairs_the_two_halves(self, crosswalk):
        pairs = read_pairs(crosswalk)
        assert len(pairs) == 2
        assert pairs[0]["anon_accession_number"] == "A0001"
        assert pairs[0]["anon_ct_accession_number"] == "A0002"

    def test_a_visit_missing_its_ct_is_dropped(self, crosswalk):
        assert "P0003" not in {pair["anon_mrn"] for pair in read_pairs(crosswalk)}

    def test_a_missing_column_raises(self, tmp_path):
        path = tmp_path / "broken.csv"
        path.write_text("anon_mrn,exam_type\nP0001,us\n")
        with pytest.raises(ValueError, match="missing required column"):
            read_pairs(path)

    def test_a_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_pairs(tmp_path / "nothing.csv")


class TestDeltaMinutes:
    """The interval survives; neither timestamp does."""

    def test_it_measures_the_gap(self):
        assert delta_minutes("2026-01-01 08:00:00", "2026-01-01 09:30:00") == 90

    def test_an_unreadable_timestamp_gives_nothing(self):
        assert delta_minutes("", "2026-01-01 09:30:00") == ""


class TestReadAnswers:
    """Only this cohort's reports are kept, and only these questions."""

    def test_it_keeps_the_cohort_and_drops_the_rest(self, answers):
        found = read_answers(answers, {"CT-1", "CT-2"}, {question_id(q) for q in QUESTIONS})
        assert set(found) == {"CT-1", "CT-2"}

    def test_it_decodes_the_three_states(self, answers):
        found = read_answers(answers, {"CT-1", "CT-2"}, {question_id(q) for q in QUESTIONS})
        assert found["CT-1"][question_id(QUESTIONS[0])] == 1
        assert found["CT-1"][question_id(QUESTIONS[1])] == 0
        assert found["CT-2"][question_id(QUESTIONS[1])] == UNANSWERED

    def test_a_question_the_config_does_not_name_is_skipped(self, answers):
        found = read_answers(answers, {"CT-1"}, {question_id(QUESTIONS[0])})
        assert set(found["CT-1"]) == {question_id(QUESTIONS[0])}


class TestBuildRows:
    """One row per pair, whether or not its report was ever extracted."""

    def test_one_row_per_pair(self, crosswalk, answers, modality_config):
        questions = read_questions(modality_config)
        pairs = read_pairs(crosswalk)
        found = read_answers(answers, {"CT-1", "CT-2"}, set(questions))
        rows, unreported = build_rows(pairs, found, questions, "v1")
        assert len(rows) == 2
        assert unreported == 0

    def test_a_pair_with_no_report_is_kept_and_unanswered(
        self, crosswalk, answers, modality_config
    ):
        # Dropping it would hide the gap from the modelling project.
        questions = read_questions(modality_config)
        pairs = read_pairs(crosswalk)
        rows, unreported = build_rows(pairs, {}, questions, "v1")
        assert unreported == 2
        for row in rows:
            labels = [value for key, value in row.items() if key.startswith(QUESTION_PREFIX)]
            assert set(labels) == {UNANSWERED}

    def test_downloaded_follows_what_is_on_disk(
        self, tmp_path, crosswalk, answers, modality_config
    ):
        arrays = tmp_path / "cohort-arrays"
        (arrays / "P0001" / "visit-01" / "us" / "A0001.zarr").mkdir(parents=True)
        questions = read_questions(modality_config)
        rows, _ = build_rows(read_pairs(crosswalk), {}, questions, "v1", arrays=arrays)
        assert [row["downloaded"] for row in rows] == [True, False]


class TestWriteLabels:
    """What lands on disk, and what must not."""

    @pytest.fixture
    def written(self, tmp_path, crosswalk, answers, modality_config):
        questions = read_questions(modality_config)
        pairs = read_pairs(crosswalk)
        found = read_answers(answers, {"CT-1", "CT-2"}, set(questions))
        rows, _ = build_rows(pairs, found, questions, config_digest(modality_config))
        return write_labels(rows, questions, tmp_path / "labels", config_digest(modality_config))

    def test_the_header_holds_one_column_per_question(self, written):
        table, _sidecar = written
        with table.open(newline="") as handle:
            header = next(csv.reader(handle))
        assert sum(name.startswith(QUESTION_PREFIX) for name in header) == len(QUESTIONS)

    def test_no_real_identifier_reaches_the_table(self, written):
        table, _sidecar = written
        text = table.read_text()
        for real in ("CT-1", "CT-2", "US-1", "US-2", "111", "222"):
            assert real not in text

    def test_no_timestamp_reaches_the_table(self, written):
        table, _sidecar = written
        assert "2026-01-01" not in table.read_text()

    def test_the_interval_does(self, written):
        table, _sidecar = written
        rows = list(csv.DictReader(table.open(newline="")))
        assert rows[0]["us_ct_delta_minutes"] == "90"

    def test_the_sidecar_says_what_each_column_means(self, written):
        _table, sidecar = written
        schema = json.loads(sidecar.read_text())
        assert set(schema["questions"]) == {question_id(q) for q in QUESTIONS}
        assert schema["questions"][question_id(QUESTIONS[0])]["question"] == QUESTIONS[0]

    def test_the_sidecar_carries_no_report_text(self, written):
        _table, sidecar = written
        assert "report_id" not in sidecar.read_text()


class TestMain:
    """The whole join, end to end, on synthetic files."""

    def test_it_writes_both_files(self, tmp_path, crosswalk, answers, modality_config):
        output = tmp_path / "labels"
        main(
            answers=str(answers),
            crosswalk=str(crosswalk),
            modality_config=str(modality_config),
            output=str(output),
        )
        assert (output / "exam_findings.csv").exists()
        assert (output / "exam_findings.json").exists()

    def test_a_dry_run_writes_nothing(self, tmp_path, crosswalk, answers, modality_config):
        output = tmp_path / "labels"
        main(
            answers=str(answers),
            crosswalk=str(crosswalk),
            modality_config=str(modality_config),
            output=str(output),
            dry_run=True,
        )
        assert not output.exists()
