"""
split.py

Description: Mark every matched ultrasound-CT pair as test, validation or
             training, and fix the order the training pairs are downloaded
             in, before any of them is downloaded.

The cohort is downloaded in batches, so the split has to exist first, on the
whole match list, and every later batch has to respect it. This writes a copy
of the match list with two more columns:

- ``split``: ``test`` for every visit of a patient with an ultrasound in the
  test year, ``val`` for a frozen sample of patients drawn from the rest,
  ``train`` for the remainder, and ``unreported`` for a visit whose CT has no
  report, which has no labels and is never downloaded.
- ``train_order``: the training pairs numbered 1 to N in a seeded random order
  of patients, each patient's visits together and in date order. A training
  stage of N exams is the first N of this order.

The validation patients are drawn by iterative stratification (Sechidis et
al., 2011) over the year of each patient's first visit and every finding the
question set includes, so the validation set gets its share of a rare finding.
A patient counts as positive for a finding if any of their visits is.

The rows come out in download order: validation, then training by
``train_order``, then test and unreported. `download-cohort` numbers patients
in the order it first sees them, so downloading ``--split val`` and then
``--split train`` assigns pseudonyms in that order too.

The output has real MRNs, accession numbers and timestamps. It is a crosswalk
in all but name: never commit it, never log a row of it, and exclude it from
any copy of the data. Everything this module logs is a count.

Examples
--------
Draw the split and print its counts without writing anything::

    pixi run split-cohort --matched_csv matched_us_ct-2019_2026.csv \
        --answers ../ifast/data/fast_ct_cohort/metadata/ct_text_reports/questions.csv \
        --dry_run

Write it beside the match list::

    pixi run split-cohort --matched_csv matched_us_ct-2019_2026.csv \
        --answers ../ifast/data/fast_ct_cohort/metadata/ct_text_reports/questions.csv \
        --output matched_us_ct-2019_2026-split.csv
"""

# Standard libraries
import csv
import json
import logging
import random
from collections import Counter, defaultdict
from pathlib import Path

# Non-standard libraries
import fire

# Custom libraries
from air_download.us_ct.cohort import REQUIRED_COLUMNS
from air_download.us_ct.labels import read_answers
from air_download.utils import parse_datetime

logger = logging.getLogger(__name__)

# The columns this adds to the match list
SPLIT_COLUMN = "split"
ORDER_COLUMN = "train_order"

SPLIT_TEST = "test"
SPLIT_VAL = "val"
SPLIT_TRAIN = "train"
SPLIT_UNREPORTED = "unreported"
SPLITS = (SPLIT_VAL, SPLIT_TRAIN, SPLIT_TEST, SPLIT_UNREPORTED)

# Every visit of a patient with an ultrasound in this year is test data
TEST_YEAR = 2026

# The tiers a FAST window could show. The question set's lock file records a
# tier per finding, and tier C is everything outside the windows.
INCLUDED_TIERS = ("A", "B")

DEFAULT_LOCK = "../rate/config/modalities/fast_abdomen_ct.lock.json"
DEFAULT_VAL_EXAMS = 1000
DEFAULT_SEED = 20261111

# The prefix a year takes among the stratification labels, so a year cannot
# collide with a finding's name
YEAR_LABEL = "year:"


def read_matched_rows(csv_path: str | Path) -> list[dict[str, str]]:
    """Read every column of the match list, one row per distinct pair.

    `read_matched_pairs` in `cohort.py` keeps only the columns a download
    needs. The split is written back as a copy of the whole list, so this
    keeps every column, and applies the same two rules: a row missing a
    required value is skipped, and an exact duplicate pair is collapsed.

    Parameters
    ----------
    csv_path : str or Path
        The match list `air_match` wrote.

    Returns
    -------
    list of dict
        One entry per usable row, in file order, every value a string.
    """
    path = Path(csv_path)
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} is missing required column(s): {', '.join(missing)}.")
        rows, seen, incomplete = [], set(), 0
        for raw in reader:
            row = {column: (value or "").strip() for column, value in raw.items()}
            key = tuple(row[c] for c in REQUIRED_COLUMNS)
            if not all(key):
                incomplete += 1
                continue
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    if incomplete:
        logger.warning("Skipped %d row(s) missing a required value.", incomplete)
    logger.info("Read %d unique matched pair(s).", len(rows))
    return rows


def included_findings(lock_path: str | Path, tiers=INCLUDED_TIERS) -> dict[str, list[str]]:
    """Read which findings the question set includes, and the questions behind each.

    Parameters
    ----------
    lock_path : str or Path
        The lock file `rate`'s generated question set has beside it.
    tiers : tuple of str, optional
        The tiers to keep.

    Returns
    -------
    dict
        Finding id to the ids of the questions that word it.
    """
    lock = json.loads(Path(lock_path).read_text())
    findings = {}
    for finding, entry in lock["findings"].items():
        if entry.get("tier") in tiers:
            findings[finding] = [variant["md5"] for variant in entry["variants"]]
    return findings


def finding_positives(
    answers: dict[str, dict[str, int]], findings: dict[str, list[str]]
) -> dict[str, set[str]]:
    """The findings each report is positive for.

    A finding is positive when any of its wordings was answered Yes, which is
    the question set's `max` rule.

    Parameters
    ----------
    answers : dict
        Report id to question id to 1, 0 or -1, from `read_answers`.
    findings : dict
        From `included_findings`.

    Returns
    -------
    dict
        Report id to the set of findings it is positive for.
    """
    return {
        report: {
            finding
            for finding, questions in findings.items()
            if any(by_question.get(question) == 1 for question in questions)
        }
        for report, by_question in answers.items()
    }


def visit_year(row: dict[str, str]) -> int | None:
    """The year of a visit's ultrasound, or None if its timestamp will not parse."""
    try:
        return parse_datetime(row.get("us_date_time") or "").year
    except ValueError:
        return None


def stratified_sample(
    labels: dict[str, set[str]], fraction: float, rng: random.Random
) -> set[str]:
    """Draw a fraction of examples so that each label keeps that fraction.

    Iterative stratification for two subsets (Sechidis, Tsoumakas and
    Vlahavas, 2011). The rarest label is placed first, each of its examples
    goes to whichever subset most needs that label, and every placement
    lowers the need of both subsets for all of the example's labels. An
    example with no labels goes to whichever subset is furthest from its
    size.

    Parameters
    ----------
    labels : dict
        Example id to its labels.
    fraction : float
        The share of examples to draw, between 0 and 1.
    rng : random.Random
        Breaks ties and orders the examples within a label.

    Returns
    -------
    set of str
        The examples drawn.
    """
    if not 0 < fraction < 1:
        raise ValueError(f"fraction must be between 0 and 1, got {fraction}.")

    shares = (fraction, 1 - fraction)
    remaining = {example: set(example_labels) for example, example_labels in labels.items()}
    examples_by_label: dict[str, set[str]] = defaultdict(set)
    for example, example_labels in remaining.items():
        for label in example_labels:
            examples_by_label[label].add(example)

    # How many more examples, and examples of each label, each subset needs
    need = [share * len(remaining) for share in shares]
    label_need = [
        {label: share * len(members) for label, members in examples_by_label.items()}
        for share in shares
    ]
    drawn: set[str] = set()

    def place(example: str, subset: int) -> None:
        if subset == 0:
            drawn.add(example)
        need[subset] -= 1
        for label in remaining[example]:
            label_need[subset][label] -= 1
            examples_by_label[label].discard(example)
        del remaining[example]

    while any(examples_by_label.values()):
        # The rarest label still unplaced; sorted first so ties are reproducible
        label = min(
            sorted(label for label, members in examples_by_label.items() if members),
            key=lambda name: len(examples_by_label[name]),
        )
        for example in rng.sample(sorted(examples_by_label[label]), len(examples_by_label[label])):
            wants = [label_need[subset][label] for subset in (0, 1)]
            if wants[0] != wants[1]:
                subset = 0 if wants[0] > wants[1] else 1
            elif need[0] != need[1]:
                subset = 0 if need[0] > need[1] else 1
            else:
                subset = rng.randrange(2)
            place(example, subset)

    for example in rng.sample(sorted(remaining), len(remaining)):
        place(example, 0 if need[0] > need[1] else 1)
    return drawn


def assign_splits(
    rows: list[dict[str, str]],
    positives: dict[str, set[str]],
    val_exams: int = DEFAULT_VAL_EXAMS,
    seed: int = DEFAULT_SEED,
    test_year: int = TEST_YEAR,
) -> list[dict[str, str]]:
    """Give every matched pair a split, and the training pairs an order.

    Parameters
    ----------
    rows : list of dict
        From `read_matched_rows`.
    positives : dict
        CT accession number to the findings its report is positive for, from
        `finding_positives`. A CT absent from it has no report.
    val_exams : int, optional
        The number of validation exams to aim for. Patients are drawn, so
        the count lands near it.
    seed : int, optional
        Seeds both the validation draw and the training order.
    test_year : int, optional
        Every visit of a patient with an ultrasound in this year is test data.

    Returns
    -------
    list of dict
        The rows, each with `split` and `train_order`, in download order.
    """
    rng = random.Random(seed)
    by_patient: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_patient[row["mrn"]].append(dict(row))

    test_patients = {
        mrn for mrn, visits in by_patient.items() if any(visit_year(v) == test_year for v in visits)
    }

    # The patients who can be drawn, labelled by their reported visits
    labels: dict[str, set[str]] = {}
    exams_per_patient: dict[str, int] = {}
    for mrn, visits in by_patient.items():
        if mrn in test_patients:
            continue
        reported = [v for v in visits if v["ct_accession_number"] in positives]
        if not reported:
            continue
        years = [year for year in map(visit_year, reported) if year is not None]
        patient_labels = set().union(*(positives[v["ct_accession_number"]] for v in reported))
        if years:
            patient_labels.add(f"{YEAR_LABEL}{min(years)}")
        labels[mrn] = patient_labels
        exams_per_patient[mrn] = len(reported)

    eligible_exams = sum(exams_per_patient.values())
    if eligible_exams <= val_exams:
        raise ValueError(
            f"Only {eligible_exams} reported exam(s) are outside the test set, "
            f"too few to draw {val_exams} for validation."
        )
    val_patients = stratified_sample(labels, val_exams / eligible_exams, rng)

    train_patients = sorted(set(labels) - val_patients)
    rng.shuffle(train_patients)
    val_order = sorted(val_patients)
    rng.shuffle(val_order)

    def by_date(visits):
        return sorted(visits, key=lambda v: (v.get("us_date_time") or "", v["ct_accession_number"]))

    out: list[dict[str, str]] = []
    for mrn in val_order:
        for visit in by_date(by_patient[mrn]):
            reported = visit["ct_accession_number"] in positives
            out.append({**visit, SPLIT_COLUMN: SPLIT_VAL if reported else SPLIT_UNREPORTED, ORDER_COLUMN: ""})

    order = 0
    for mrn in train_patients:
        for visit in by_date(by_patient[mrn]):
            if visit["ct_accession_number"] in positives:
                order += 1
                out.append({**visit, SPLIT_COLUMN: SPLIT_TRAIN, ORDER_COLUMN: str(order)})
            else:
                out.append({**visit, SPLIT_COLUMN: SPLIT_UNREPORTED, ORDER_COLUMN: ""})

    placed = set(val_order) | set(train_patients)
    for mrn, visits in by_patient.items():
        if mrn in placed:
            continue
        split = SPLIT_TEST if mrn in test_patients else SPLIT_UNREPORTED
        for visit in by_date(visits):
            out.append({**visit, SPLIT_COLUMN: split, ORDER_COLUMN: ""})
    return out


def split_counts(rows: list[dict[str, str]], positives: dict[str, set[str]], findings) -> dict:
    """Count patients, exams, years and finding positives in each split.

    Parameters
    ----------
    rows : list of dict
        From `assign_splits`.
    positives : dict
        From `finding_positives`, keyed by CT accession number.
    findings : iterable of str
        The finding ids to count.

    Returns
    -------
    dict
        ``patients``, ``exams``, ``years`` and ``findings``, each keyed by split.
        Every value is a count.
    """
    patients: dict[str, set[str]] = defaultdict(set)
    exams: Counter = Counter()
    years: dict[str, Counter] = defaultdict(Counter)
    finding_counts: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        split = row[SPLIT_COLUMN]
        patients[split].add(row["mrn"])
        exams[split] += 1
        years[split][visit_year(row)] += 1
        for finding in positives.get(row["ct_accession_number"], ()):
            finding_counts[split][finding] += 1
    return {
        "patients": {split: len(members) for split, members in patients.items()},
        "exams": dict(exams),
        "years": {split: dict(counter) for split, counter in years.items()},
        "findings": {
            finding: {split: finding_counts[split][finding] for split in (SPLIT_VAL, SPLIT_TRAIN)}
            for finding in findings
        },
    }


def write_split(rows: list[dict[str, str]], output: str | Path) -> Path:
    """Write the split match list, keeping the input's columns.

    Parameters
    ----------
    rows : list of dict
        From `assign_splits`.
    output : str or Path
        The CSV to write.

    Returns
    -------
    Path
        The file written.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = [column for column in rows[0] if column not in (SPLIT_COLUMN, ORDER_COLUMN)]
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*columns, SPLIT_COLUMN, ORDER_COLUMN])
        writer.writeheader()
        writer.writerows(rows)
    return output


def log_counts(counts: dict) -> None:
    """Log what `split_counts` returned. Every value is a count."""
    for split in SPLITS:
        logger.info(
            "%-10s %6d patient(s), %6d exam(s)",
            split,
            counts["patients"].get(split, 0),
            counts["exams"].get(split, 0),
        )
    all_years = sorted({year for by_year in counts["years"].values() for year in by_year if year})
    logger.info("Exams per year, val / train / test:")
    for year in all_years:
        logger.info(
            "  %d  %5d / %5d / %5d",
            year,
            *(counts["years"].get(split, {}).get(year, 0) for split in (SPLIT_VAL, SPLIT_TRAIN, SPLIT_TEST)),
        )
    logger.info("Positive exams per finding, val / train:")
    for finding, by_split in sorted(counts["findings"].items()):
        logger.info("  %-55s %4d / %5d", finding, by_split[SPLIT_VAL], by_split[SPLIT_TRAIN])


def main(
    matched_csv: str,
    answers: str,
    lock: str = DEFAULT_LOCK,
    output: str | None = None,
    val_exams: int = DEFAULT_VAL_EXAMS,
    seed: int = DEFAULT_SEED,
    test_year: int = TEST_YEAR,
    dry_run: bool = False,
) -> None:
    """Split the match list into test, validation and training, and order the training pairs.

    Parameters
    ----------
    matched_csv : str
        The match list `air_match` wrote.
    answers : str
        RATE's `questions.csv`, keyed by the CT's accession number.
    lock : str, optional
        The question set's lock file, which names the included findings.
    output : str, optional
        Where to write the split match list. Defaults to ``<matched_csv>-split.csv``.
    val_exams : int, optional
        The number of validation exams to aim for.
    seed : int, optional
        Seeds the draw and the order. Record it; the split is frozen once drawn.
    test_year : int, optional
        The year whose patients are held out as test data.
    dry_run : bool, optional
        Print the counts and write nothing.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    matched = Path(matched_csv)
    destination = Path(output) if output else matched.with_name(f"{matched.stem}-split.csv")
    if not dry_run and destination.exists():
        raise FileExistsError(
            f"{destination} exists. The split is frozen once drawn; remove it "
            "only if nothing has been downloaded from it."
        )

    findings = included_findings(lock)
    questions = {question for variants in findings.values() for question in variants}
    logger.info("%d included finding(s) over %d question(s).", len(findings), len(questions))

    rows = read_matched_rows(matched)
    wanted = {row["ct_accession_number"] for row in rows}
    positives = finding_positives(read_answers(answers, wanted, questions), findings)

    split = assign_splits(rows, positives, val_exams=val_exams, seed=seed, test_year=test_year)
    log_counts(split_counts(split, positives, findings))
    logger.info("Seed %d, test year %d, %d validation exam(s) requested.", seed, test_year, val_exams)

    if dry_run:
        logger.info("Dry run: nothing written.")
        return
    write_split(split, destination)
    logger.info(
        "Wrote %d row(s) to %s. It holds real identifiers: keep it out of git, "
        "out of logs and out of anything shared.",
        len(split),
        destination,
    )


def cli() -> None:
    """Entry point for `air_split`."""
    fire.Fire(main)


if __name__ == "__main__":
    cli()
