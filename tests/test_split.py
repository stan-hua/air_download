"""Tests for splitting the match list and ordering the training pairs.

Every identifier, date and answer here is synthetic.
"""

# Standard libraries
import csv
import json
import random
from collections import Counter
from pathlib import Path

# Non-standard libraries
import pytest

# Custom libraries
from air_download.crosswalk import CROSSWALK_CSV_HEADER
from air_download.us_ct.cohort import download_cohort
from air_download.us_ct.labels import build_rows, read_pairs, read_splits
from air_download.us_ct.split import (
    ORDER_COLUMN,
    SPLIT_COLUMN,
    assign_splits,
    finding_positives,
    included_findings,
    main,
    read_matched_rows,
    split_counts,
    stratified_sample,
    write_split,
)

MATCH_HEADER = [
    "mrn",
    "us_accession_number",
    "us_date_time",
    "ct_accession_number",
    "ct_date_time",
    "us_image_count",
]


def visit(mrn: str, index: int, year: int) -> dict[str, str]:
    """One synthetic matched pair."""
    return {
        "mrn": mrn,
        "us_accession_number": f"U-{mrn}-{index}",
        "us_date_time": f"{year}-03-0{index}T08:00:00-08:00",
        "ct_accession_number": f"C-{mrn}-{index}",
        "ct_date_time": f"{year}-03-0{index}T10:00:00-08:00",
        "us_image_count": "4",
    }


def cohort(n_patients: int = 60, seed: int = 0) -> list[dict[str, str]]:
    """Patients with one to three visits each, spread over 2019 to 2026."""
    rng = random.Random(seed)
    rows = []
    for patient in range(n_patients):
        mrn = f"{patient:05d}"
        year = rng.randrange(2019, 2027)
        for index in range(1, rng.randrange(2, 5)):
            rows.append(visit(mrn, index, year))
    return rows


def reported(rows, findings=("ascites", "hydronephrosis"), seed=0) -> dict[str, set[str]]:
    """Every CT reported, each positive for a finding about a third of the time."""
    rng = random.Random(seed)
    return {
        row["ct_accession_number"]: {f for f in findings if rng.random() < 0.3} for row in rows
    }


class TestIncludedFindings:
    def test_keeps_tiers_a_and_b(self, tmp_path):
        lock = tmp_path / "set.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "findings": {
                        "ascites": {"tier": "A", "variants": [{"md5": "aaaa0001"}, {"md5": "aaaa0002"}]},
                        "cyst": {"tier": "B", "variants": [{"md5": "bbbb0001"}]},
                        "pancreas": {"tier": "C", "variants": [{"md5": "cccc0001"}]},
                    }
                }
            )
        )
        assert included_findings(lock) == {
            "ascites": ["aaaa0001", "aaaa0002"],
            "cyst": ["bbbb0001"],
        }


class TestFindingPositives:
    def test_any_wording_answered_yes_is_positive(self):
        findings = {"ascites": ["q1", "q2"], "cyst": ["q3"]}
        answers = {"C1": {"q1": 0, "q2": 1, "q3": -1}, "C2": {"q1": -1, "q3": 0}}
        assert finding_positives(answers, findings) == {"C1": {"ascites"}, "C2": set()}


class TestStratifiedSample:
    def test_draws_close_to_the_fraction(self):
        rng = random.Random(1)
        labels = {f"e{i}": ({"rare"} if i < 10 else set()) | {"common"} for i in range(200)}
        drawn = stratified_sample(labels, 0.2, rng)
        assert 38 <= len(drawn) <= 42

    def test_a_rare_label_gets_its_share(self):
        rng = random.Random(2)
        labels = {f"e{i}": ({"rare"} if i < 10 else set()) for i in range(200)}
        drawn = stratified_sample(labels, 0.2, rng)
        assert sum(1 for e in drawn if "rare" in labels[e]) == 2

    def test_is_reproducible_under_a_seed(self):
        labels = {f"e{i}": {f"l{i % 7}"} for i in range(100)}
        first = stratified_sample(labels, 0.3, random.Random(5))
        second = stratified_sample(labels, 0.3, random.Random(5))
        assert first == second

    def test_rejects_a_fraction_outside_zero_and_one(self):
        with pytest.raises(ValueError):
            stratified_sample({"e": set()}, 1.0, random.Random(0))


class TestAssignSplits:
    def test_every_visit_of_a_test_patient_is_test(self):
        rows = [visit("1", 1, 2024), visit("1", 2, 2026), visit("2", 1, 2024)] + cohort(40)
        out = assign_splits(rows, reported(rows), val_exams=10, seed=0)
        assert {row[SPLIT_COLUMN] for row in out if row["mrn"] == "1"} == {"test"}

    def test_a_patient_is_in_one_split(self):
        rows = cohort(80)
        out = assign_splits(rows, reported(rows), val_exams=20, seed=0)
        splits_per_patient = {}
        for row in out:
            splits_per_patient.setdefault(row["mrn"], set()).add(row[SPLIT_COLUMN])
        assert all(len(splits) == 1 for splits in splits_per_patient.values())

    def test_an_unreported_ct_is_never_downloaded(self):
        rows = cohort(40)
        positives = reported(rows)
        missing = rows[0]["ct_accession_number"]
        del positives[missing]
        out = assign_splits(rows, positives, val_exams=10, seed=0)
        (row,) = [row for row in out if row["ct_accession_number"] == missing]
        assert row[SPLIT_COLUMN] == "unreported"
        assert row[ORDER_COLUMN] == ""

    def test_training_order_counts_up_with_each_patient_together(self):
        rows = cohort(80)
        out = assign_splits(rows, reported(rows), val_exams=20, seed=0)
        train = [row for row in out if row[SPLIT_COLUMN] == "train"]
        assert [int(row[ORDER_COLUMN]) for row in train] == list(range(1, len(train) + 1))
        patients_in_order = [row["mrn"] for row in train]
        changes = sum(1 for a, b in zip(patients_in_order, patients_in_order[1:]) if a != b)
        assert changes == len(set(patients_in_order)) - 1

    def test_rows_come_out_in_download_order(self):
        rows = cohort(80)
        out = assign_splits(rows, reported(rows), val_exams=20, seed=0)
        order = {"val": 0, "train": 1, "test": 2, "unreported": 2}
        ranks = [order[row[SPLIT_COLUMN]] for row in out]
        assert ranks == sorted(ranks)

    def test_the_same_seed_gives_the_same_split(self):
        rows = cohort(80)
        first = assign_splits(rows, reported(rows), val_exams=20, seed=3)
        second = assign_splits(list(reversed(rows)), reported(rows), val_exams=20, seed=3)
        key = lambda out: sorted((r["ct_accession_number"], r[SPLIT_COLUMN], r[ORDER_COLUMN]) for r in out)  # noqa: E731
        assert key(first) == key(second)

    def test_too_few_exams_raises(self):
        rows = cohort(5)
        with pytest.raises(ValueError, match="too few"):
            assign_splits(rows, reported(rows), val_exams=1000, seed=0)


class TestSplitCounts:
    def test_counts_only(self):
        rows = cohort(60)
        positives = reported(rows)
        out = assign_splits(rows, positives, val_exams=15, seed=0)
        counts = split_counts(out, positives, ["ascites"])
        assert sum(counts["exams"].values()) == len(rows)
        assert set(counts["findings"]["ascites"]) == {"val", "train"}


class TestReadMatchedRows:
    def test_keeps_every_column_and_collapses_duplicates(self, tmp_path):
        rows = [visit("1", 1, 2024), visit("1", 1, 2024)]
        path = tmp_path / "m.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MATCH_HEADER)
            writer.writeheader()
            writer.writerows(rows)
        (row,) = read_matched_rows(path)
        assert row["us_image_count"] == "4"

    def test_identifiers_keep_their_leading_zeros(self, tmp_path):
        path = tmp_path / "m.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MATCH_HEADER)
            writer.writeheader()
            writer.writerow(visit("00123", 1, 2024))
        assert read_matched_rows(path)[0]["mrn"] == "00123"


@pytest.fixture
def split_file(tmp_path):
    """A split match list with every split present."""
    rows = cohort(80)
    out = assign_splits(rows, reported(rows), val_exams=20, seed=0)
    return write_split(out, tmp_path / "matched-split.csv"), out


class TestDownloadBySplit:
    def test_only_the_split_is_downloaded(self, tmp_path, split_file, capsys):
        path, out = split_file
        download_cohort(matched_csv=path, output=tmp_path / "out", split="val", dry_run=True)
        printed = [line for line in capsys.readouterr().out.splitlines() if line.endswith(".zip")]
        assert len(printed) == 2 * sum(1 for row in out if row[SPLIT_COLUMN] == "val")

    def test_n_counts_within_the_split(self, tmp_path, split_file, capsys):
        path, _ = split_file
        download_cohort(matched_csv=path, output=tmp_path / "out", split="train", n=7, dry_run=True)
        printed = [line for line in capsys.readouterr().out.splitlines() if line.endswith(".zip")]
        assert len(printed) == 14

    def test_a_list_without_a_split_column_raises(self, tmp_path):
        path = tmp_path / "m.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MATCH_HEADER)
            writer.writeheader()
            writer.writerow(visit("1", 1, 2024))
        with pytest.raises(ValueError, match="air_split"):
            download_cohort(matched_csv=path, output=tmp_path / "out", split="val", dry_run=True)


class TestLabelsCarryTheSplit:
    def test_split_and_order_reach_the_table(self, tmp_path):
        split_rows = [
            {**visit("111", 1, 2024), SPLIT_COLUMN: "train", ORDER_COLUMN: "1"},
            {**visit("222", 1, 2024), SPLIT_COLUMN: "val", ORDER_COLUMN: ""},
        ]
        split_path = write_split(split_rows, tmp_path / "split.csv")

        crosswalk = tmp_path / "cohort_crosswalk.csv"
        with crosswalk.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(CROSSWALK_CSV_HEADER)
            for p, row in enumerate(split_rows, start=1):
                writer.writerow(
                    (f"P000{p}", f"A00{p}1", "visit-01", "us", "u.zip", row["mrn"],
                     row["us_accession_number"], row["us_date_time"])
                )
                writer.writerow(
                    (f"P000{p}", f"A00{p}2", "visit-01", "ct", "c.zip", row["mrn"],
                     row["ct_accession_number"], row["ct_date_time"])
                )

        rows, _ = build_rows(read_pairs(crosswalk), {}, {}, "v", splits=read_splits(split_path))
        assert {(r["anon_mrn"], r["split"], r["train_order"]) for r in rows} == {
            ("P0001", "train", "1"),
            ("P0002", "val", ""),
        }
        # The join key stays out of the table
        assert all("mrn" not in row and "us_accession_number" not in row for row in rows)


class TestMain:
    def test_refuses_to_overwrite_a_drawn_split(self, tmp_path):
        existing = tmp_path / "split.csv"
        existing.write_text("already drawn\n")
        with pytest.raises(FileExistsError):
            main(matched_csv=str(tmp_path / "m.csv"), answers="unused", output=str(existing))

    def test_writes_a_split_from_synthetic_inputs(self, tmp_path):
        rows = cohort(80)
        matched = tmp_path / "m.csv"
        with matched.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MATCH_HEADER)
            writer.writeheader()
            writer.writerows(rows)
        lock = tmp_path / "set.lock.json"
        lock.write_text(json.dumps({"findings": {"ascites": {"tier": "A", "variants": [{"md5": "q1"}]}}}))
        answers = tmp_path / "questions.csv"
        with answers.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["report_id", "category", "question_id", "question", "answer"])
            for i, row in enumerate(rows):
                writer.writerow([row["ct_accession_number"], "Peritoneum", "q1", "x", "Yes" if i % 4 == 0 else "No"])

        output = tmp_path / "m-split.csv"
        main(str(matched), str(answers), lock=str(lock), output=str(output), val_exams=20, seed=0)
        with output.open(newline="") as handle:
            written = list(csv.DictReader(handle))
        assert len(written) == len(rows)
        assert set(Counter(row[SPLIT_COLUMN] for row in written)) <= {"val", "train", "test"}
        assert written[0]["us_image_count"] == "4"
