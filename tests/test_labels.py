"""Tests for writing a cohort's CT findings keyed by pseudonym.

Every identifier and every report answer here is synthetic. The point of most
of these tests is that nothing real survives the join: the table has to carry
the pseudonyms and the labels, and neither the accession number it joined on
nor the timestamps it read.
"""

# Standard libraries
import csv
import hashlib
import inspect
import json
from pathlib import Path

# Non-standard libraries
import pytest
import yaml

# Custom libraries
from air_download.crosswalk import CROSSWALK_CSV_HEADER
from air_download.us_ct.labels import (
    DISCORD_PREFIX,
    QUESTION_PREFIX,
    UNANSWERED,
    VOTE_COUNT_PREFIX,
    build_rows,
    config_digest,
    decode_digest,
    delta_minutes,
    main,
    parse_answer,
    read_answers,
    read_pairs,
    read_questions,
    vote,
    write_labels,
)

# Two findings, one asked under two wordings and one under three, because a
# single Yes means something different at each and `nvote_` is what tells them
# apart.
FINDINGS = {
    "ascites": {
        "category": "Peritoneum",
        "question": "Is there any ascites?",
        "variants": ["According to the report, is there any ascites?"],
    },
    "hepatic_cysts_simple": {
        "category": "Liver",
        "question": "Are there any hepatic cysts?",
        "variants": [
            "Are there simple liver cysts?",
            "According to the report, are there any hepatic cysts?",
        ],
    },
}


@pytest.fixture
def modality_config(tmp_path):
    """A two-finding modality config, shaped the way the generated one is."""
    path = tmp_path / "abdomen_ct.yaml"
    categories: dict = {}
    for identifier, entry in FINDINGS.items():
        categories.setdefault(entry["category"], {"questions": []})["questions"].append(
            {
                "id": identifier,
                "question": entry["question"],
                "variants": entry["variants"],
            }
        )
    path.write_text(yaml.safe_dump({"modality": "abdominal_ct", "categories": categories}))
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


ANSWER_COLUMNS = [
    "report_id", "category", "finding_id", "variant_index",
    "question_id", "question", "answer", "template",
    "trauma_indication", "trauma_terms",
    "verdict", "evidence", "certainty_phrase", "certainty",
    "severity_phrase", "severity", "attribution", "named_outright",
]


def _wording_md5(text):
    """What rate writes into the `question_id` column. Nothing here reads it."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]


def _answer_row(report, finding, index, verdict, template="A", answer="", trauma="yes"):
    """One row of `questions.csv`, with the columns rate writes."""
    entry = FINDINGS[finding]
    wordings = [entry["question"], *entry["variants"]]
    text = wordings[index]
    return {
        "report_id": report,
        "category": entry["category"],
        "finding_id": finding,
        "variant_index": index,
        "question_id": _wording_md5(text),
        "question": text,
        "answer": answer or f'{{"answer": "{verdict}"}}',
        "template": template,
        "trauma_indication": trauma,
        "trauma_terms": "vehicle" if trauma == "yes" else "",
        "verdict": verdict,
        "evidence": "a sentence the report never really said",
        "certainty_phrase": "",
        "certainty": "stated" if verdict == "yes" else "",
        "severity_phrase": "",
        "severity": "mild" if verdict == "yes" else "",
        "attribution": "unknown",
        "named_outright": "true" if verdict == "yes" else "false",
    }


@pytest.fixture
def answers(tmp_path):
    """Answers for both downloaded CT reports, plus one nobody downloaded.

    CT-1 agrees on both findings. CT-2 is the discordant case: its ascites
    wordings split one Yes against one No, and its hepatic cysts have one
    wording that returned nothing at all, which the vote ignores because the
    other two answered.
    """
    path = tmp_path / "questions.csv"
    rows = [
        _answer_row("CT-1", "ascites", 0, "yes"),
        _answer_row("CT-1", "ascites", 1, "yes"),
        _answer_row("CT-1", "hepatic_cysts_simple", 0, "no"),
        _answer_row("CT-1", "hepatic_cysts_simple", 1, "no"),
        _answer_row("CT-1", "hepatic_cysts_simple", 2, "no"),
        _answer_row("CT-2", "ascites", 0, "yes", template="B", trauma="no"),
        _answer_row("CT-2", "ascites", 1, "no", template="B", trauma="no"),
        _answer_row("CT-2", "hepatic_cysts_simple", 0, "no", template="B"),
        _answer_row("CT-2", "hepatic_cysts_simple", 1, "not_assessed", template="B"),
        _answer_row("CT-2", "hepatic_cysts_simple", 2, "no", template="B"),
        _answer_row("CT-9", "ascites", 0, "yes"),
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANSWER_COLUMNS)
        writer.writeheader()
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
    """Only this cohort's reports are kept, and every wording is kept apart."""

    def keys(self):
        return {"CT-1", "CT-2"}, set(FINDINGS)

    def test_it_keeps_the_cohort_and_drops_the_rest(self, answers):
        found, _templates, _levels = read_answers(answers, *self.keys())
        assert set(found) == {"CT-1", "CT-2"}

    def test_each_wording_is_kept_under_its_own_index(self, answers):
        found, _templates, _levels = read_answers(answers, *self.keys())
        assert found["CT-1"]["ascites"] == {0: 1, 1: 1}
        assert found["CT-2"]["ascites"] == {0: 1, 1: 0}

    def test_not_assessed_is_the_third_state(self, answers):
        """The report states it cannot answer, so the cell leaves the loss.

        Folding it into a No would make rate's deferral rule a no-op, since
        that rule only ever moves a No.
        """
        found, _templates, _levels = read_answers(answers, *self.keys())
        assert found["CT-2"]["hepatic_cysts_simple"] == {0: 0, 1: UNANSWERED, 2: 0}

    def test_a_finding_the_config_does_not_name_is_skipped(self, answers):
        found, _templates, _levels = read_answers(answers, {"CT-1"}, {"ascites"})
        assert set(found["CT-1"]) == {"ascites"}

    def test_it_reads_the_covariates(self, answers):
        """One entry per report, holding whatever covariate columns it has.

        A second covariate arrived with the trauma flag, so the entry is a
        dict rather than one string. `CT-2`'s first two rows say `no` and its
        later rows say `yes`, and the first value read wins, which is what
        `setdefault` is for: a covariate describes the report, so the rows
        cannot disagree about it without the extraction being wrong.
        """
        _found, covariates, _levels = read_answers(answers, *self.keys())
        assert covariates["CT-1"]["template"] == "A"
        assert covariates["CT-1"]["trauma_indication"] == "yes"
        assert covariates["CT-1"]["trauma_terms"] == "vehicle"
        assert covariates["CT-2"]["template"] == "B"
        assert covariates["CT-2"]["trauma_indication"] == "no"

    def test_a_file_without_finding_ids_raises(self, tmp_path):
        """An extraction predating the re-key. Its columns are named for a
        wording hash, and no vote can be taken over them."""
        path = tmp_path / "old_questions.csv"
        path.write_text("report_id,question_id,question,answer\nCT-1,abc,Is there?,Yes\n")
        with pytest.raises(ValueError, match="finding_id"):
            read_answers(path, {"CT-1"}, {"ascites"})

    def test_it_falls_back_to_the_free_text_verdict(self, tmp_path):
        """`verdict` arrived with the structured answer. Before it, the label
        came off the free text, and `parse_answer` is still that reader."""
        path = tmp_path / "questions.csv"
        path.write_text(
            "report_id,finding_id,variant_index,verdict,answer\n"
            "CT-1,ascites,0,,No.\n"
            "CT-1,ascites,1,,<think> unclosed\n"
        )
        found, _templates, _levels = read_answers(path, {"CT-1"}, {"ascites"})
        assert found["CT-1"]["ascites"] == {0: 0, 1: UNANSWERED}


class TestTheVote:
    """`max` over the wordings asked, which the ADR settles over a majority."""

    def test_any_yes_wins(self):
        assert vote({0: 1, 1: 0}, 2) == (1, 1, 1)

    def test_agreement_is_not_discordant(self):
        assert vote({0: 1, 1: 1}, 2) == (1, 2, 0)
        assert vote({0: 0, 1: 0}, 2) == (0, 0, 0)

    def test_unanswered_is_ignored_unless_every_wording_returned_it(self):
        """Two wordings saying No and a third blank is a No, not an unknown."""
        assert vote({0: 0, 1: UNANSWERED, 2: 0}, 3) == (0, 0, 0)
        assert vote({0: UNANSWERED, 1: UNANSWERED}, 2) == (UNANSWERED, 0, 0)

    def test_a_finding_nobody_answered_is_unanswered(self):
        assert vote({}, 2) == (UNANSWERED, 0, 0)

    def test_a_yes_resting_on_fewer_wordings_than_asked_is_discordant(self):
        """What A30 routes into these columns: the third pilot had 1,263
        discordant cells in 16,713 positive ones."""
        assert vote({0: 1, 1: 0, 2: 0}, 3) == (1, 1, 1)
        assert vote({0: 1, 1: 1, 2: 0}, 3) == (1, 2, 1)
        assert vote({0: 1, 1: 1, 2: 1}, 3) == (1, 3, 0)


class TestBuildRows:
    """One row per pair, whether or not its report was ever extracted."""

    def built(self, crosswalk, answers, modality_config, **kwargs):
        questions = read_questions(modality_config)
        found, covariates, levels = read_answers(answers, {"CT-1", "CT-2"}, set(questions))
        rows, variants, unreported = build_rows(
            read_pairs(crosswalk), found, questions, "v1",
            covariates=covariates, levels=levels, **kwargs
        )
        return questions, rows, variants, unreported

    def test_one_row_per_pair(self, crosswalk, answers, modality_config):
        _q, rows, _v, unreported = self.built(crosswalk, answers, modality_config)
        assert len(rows) == 2
        assert unreported == 0

    def test_three_columns_per_finding(self, crosswalk, answers, modality_config):
        _q, rows, _v, _u = self.built(crosswalk, answers, modality_config)
        row = rows[0]
        assert row[f"{QUESTION_PREFIX}ascites"] == 1
        assert row[f"{VOTE_COUNT_PREFIX}ascites"] == 2
        assert row[f"{DISCORD_PREFIX}ascites"] == 0

    def test_the_discordant_pair_is_flagged(self, crosswalk, answers, modality_config):
        _q, rows, _v, _u = self.built(crosswalk, answers, modality_config)
        row = next(r for r in rows if r["anon_mrn"] == "P0002")
        assert row[f"{QUESTION_PREFIX}ascites"] == 1
        assert row[f"{VOTE_COUNT_PREFIX}ascites"] == 1
        assert row[f"{DISCORD_PREFIX}ascites"] == 1

    def test_the_template_travels(self, crosswalk, answers, modality_config):
        _q, rows, _v, _u = self.built(crosswalk, answers, modality_config)
        assert [row["template"] for row in rows] == ["A", "B"]

    def test_one_long_form_row_per_wording_answered(
        self, crosswalk, answers, modality_config
    ):
        _q, _rows, variants, _u = self.built(crosswalk, answers, modality_config)
        assert len(variants) == 10
        ascites = [v for v in variants if v["finding_id"] == "ascites"]
        assert sorted((v["anon_mrn"], v["variant_index"], v["label"]) for v in ascites) == [
            ("P0001", 0, 1),
            ("P0001", 1, 1),
            ("P0002", 0, 1),
            ("P0002", 1, 0),
        ]

    def test_a_pair_with_no_report_is_kept_and_unanswered(
        self, crosswalk, answers, modality_config
    ):
        # Dropping it would hide the gap from the modelling project.
        questions = read_questions(modality_config)
        rows, variants, unreported = build_rows(read_pairs(crosswalk), {}, questions, "v1")
        assert unreported == 2
        assert variants == []
        for row in rows:
            labels = [value for key, value in row.items() if key.startswith(QUESTION_PREFIX)]
            assert set(labels) == {UNANSWERED}

    def test_downloaded_follows_what_is_on_disk(
        self, tmp_path, crosswalk, answers, modality_config
    ):
        arrays = tmp_path / "cohort-arrays"
        (arrays / "P0001" / "visit-01" / "us" / "A0001.zarr").mkdir(parents=True)
        questions = read_questions(modality_config)
        rows, _v, _u = build_rows(
            read_pairs(crosswalk), {}, questions, "v1", arrays=arrays
        )
        assert [row["downloaded"] for row in rows] == [True, False]


class TestTheDecodeDigest:
    """A17 asked the labels to record the configuration they depend on."""

    def test_it_reads_the_manifest_beside_the_answers(self, tmp_path):
        (tmp_path / "run_manifest.json").write_text(
            json.dumps({"decode": {"enable_thinking": True, "temperature": 0}})
        )
        (tmp_path / "questions.csv").write_text("report_id,finding_id\n")
        digest, decode = decode_digest(tmp_path / "questions.csv")
        assert len(digest) == 8
        assert decode["enable_thinking"] is True

    def test_the_digest_moves_when_the_decode_does(self, tmp_path):
        (tmp_path / "questions.csv").write_text("report_id,finding_id\n")
        manifest = tmp_path / "run_manifest.json"
        manifest.write_text(json.dumps({"decode": {"enable_thinking": True}}))
        first, _ = decode_digest(tmp_path / "questions.csv")
        manifest.write_text(json.dumps({"decode": {"enable_thinking": False}}))
        second, _ = decode_digest(tmp_path / "questions.csv")
        assert first != second

    def test_key_order_does_not_move_it(self, tmp_path):
        (tmp_path / "questions.csv").write_text("report_id,finding_id\n")
        manifest = tmp_path / "run_manifest.json"
        manifest.write_text(json.dumps({"decode": {"a": 1, "b": 2}}))
        first, _ = decode_digest(tmp_path / "questions.csv")
        manifest.write_text(json.dumps({"decode": {"b": 2, "a": 1}}))
        second, _ = decode_digest(tmp_path / "questions.csv")
        assert first == second

    def test_no_manifest_leaves_it_empty(self, tmp_path):
        (tmp_path / "questions.csv").write_text("report_id,finding_id\n")
        assert decode_digest(tmp_path / "questions.csv") == ("", {})


class TestWriteLabels:
    """What lands on disk, and what must not."""

    @pytest.fixture
    def written(self, tmp_path, crosswalk, answers, modality_config):
        questions = read_questions(modality_config)
        found, covariates, levels = read_answers(answers, {"CT-1", "CT-2"}, set(questions))
        version = config_digest(modality_config)
        rows, variants, _ = build_rows(
            read_pairs(crosswalk), found, questions, version,
            covariates=covariates, levels=levels,
        )
        return write_labels(
            rows, questions, tmp_path / "labels", version, variants=variants
        )

    def test_the_header_holds_three_columns_per_finding(self, written):
        table, _sidecar, _long = written
        with table.open(newline="") as handle:
            header = next(csv.reader(handle))
        for prefix in (QUESTION_PREFIX, VOTE_COUNT_PREFIX, DISCORD_PREFIX):
            assert sum(name.startswith(prefix) for name in header) == len(FINDINGS)

    def test_there_is_no_second_label_column(self, written):
        """A5 rejected reporting the canonical answer and the vote side by
        side. `q_` is the vote and nothing holds the canonical alone."""
        table, _sidecar, _long = written
        with table.open(newline="") as handle:
            header = next(csv.reader(handle))
        assert not [name for name in header if name.startswith("vote_")]

    def test_no_real_identifier_reaches_the_table(self, written):
        table, _sidecar, long_form = written
        for path in (table, long_form):
            text = path.read_text()
            for real in ("CT-1", "CT-2", "US-1", "US-2", "111", "222"):
                assert real not in text

    def test_no_timestamp_reaches_the_table(self, written):
        table, _sidecar, _long = written
        assert "2026-01-01" not in table.read_text()

    def test_the_interval_does(self, written):
        table, _sidecar, _long = written
        rows = list(csv.DictReader(table.open(newline="")))
        assert rows[0]["us_ct_delta_minutes"] == "90"

    def test_the_sidecar_says_what_each_column_means(self, written):
        _table, sidecar, _long = written
        schema = json.loads(sidecar.read_text())
        assert set(schema["findings"]) == set(FINDINGS)
        assert schema["findings"]["ascites"]["question"] == FINDINGS["ascites"]["question"]
        assert schema["findings"]["ascites"]["n_wordings"] == 2
        assert set(schema["column_prefixes"]) == {
            QUESTION_PREFIX, VOTE_COUNT_PREFIX, DISCORD_PREFIX
        }
        assert "max" in schema["vote_rule"]

    def test_the_sidecar_carries_no_report_text(self, written):
        _table, sidecar, _long = written
        assert "report_id" not in sidecar.read_text()

    def test_the_mapped_levels_reach_the_long_form_table(self, written):
        """Q16 applies the severity threshold when a claim is made rather than
        baking it into the label, which needs the level beside each wording.
        `ascites` agrees at J = 0.944 on any Yes and 0.987 on
        moderate-or-marked, and 77% of its positives are trace or mild."""
        _table, _sidecar, long_form = written
        rows = list(csv.DictReader(long_form.open(newline="")))
        yes = [r for r in rows if r["label"] == "1"]
        assert yes and all(r["certainty"] == "stated" for r in yes)
        assert all(r["severity"] == "mild" for r in yes)

    def test_no_report_text_reaches_the_long_form_table(self, written):
        """The evidence and the two quoted phrases are verbatim report
        sentences. Only the mapped levels could ever travel, and the
        modelling project does not need even those keyed by wording."""
        _table, _sidecar, long_form = written
        with long_form.open(newline="") as handle:
            header = next(csv.reader(handle))
        for column in ("evidence", "certainty_phrase", "severity_phrase", "question"):
            assert column not in header
        assert "never really said" not in long_form.read_text()


class TestMain:
    """The whole join, end to end, on synthetic files."""

    def test_it_writes_every_file(self, tmp_path, crosswalk, answers, modality_config):
        output = tmp_path / "labels"
        main(
            answers=str(answers),
            crosswalk=str(crosswalk),
            modality_config=str(modality_config),
            output=str(output),
        )
        assert (output / "exam_findings.csv").exists()
        assert (output / "exam_findings.json").exists()
        assert (output / "exam_findings_variants.csv").exists()

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


class TestTheQuestionSetItReads:
    """Which config the labeller defaults to, and how it is versioned."""

    def test_the_default_is_the_generated_fast_set(self):
        """Not `abdomen_ct.yaml`.

        The source config asks 226 questions about anatomy no FAST window
        covers, and it has no authored finding ids at all, so defaulting to it
        now raises rather than writing a table that is wrong by 112 columns.
        """
        default = inspect.signature(main).parameters["modality_config"].default
        assert default.endswith("fast_abdomen_ct.yaml")

    def test_the_version_comes_from_the_lock_when_there_is_one(self, tmp_path):
        """`question_set` moves when a question moves and not otherwise.

        A digest of the file's bytes moves when a definition is edited or a
        comment reflowed, neither of which changes what a stored answer means,
        so a table stamped that way looks stale when it is not.
        """
        config = tmp_path / "fast_abdomen_ct.yaml"
        config.write_text("modality: x\ncategories: {}\n")
        lock = tmp_path / "fast_abdomen_ct.lock.json"
        lock.write_text(json.dumps({"digest": {"question_set": "abcdef0123456789"}}))
        assert config_digest(config) == "fast_abdomen_ct@abcdef0123456789"

        # editing the file without moving a question leaves the version alone
        config.write_text("modality: x\ncategories: {}\n# a comment\n")
        assert config_digest(config) == "fast_abdomen_ct@abcdef0123456789"

    def test_a_config_with_no_lock_falls_back_to_its_bytes(self, tmp_path):
        """Every modality config except the generated one."""
        config = tmp_path / "abdomen_ct.yaml"
        config.write_text("modality: x\ncategories: {}\n")
        first = config_digest(config)
        assert first.startswith("abdomen_ct@")
        config.write_text("modality: x\ncategories: {}\n# changed\n")
        assert config_digest(config) != first
