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

# Marks the one exam per patient that a download pulls. A patient contributes
# one pair, so every analysis treats exams as independent and no figure has to
# be averaged within a patient first. `ifast/docs/PRETRAINING.md` records what
# the restriction costs.
FIRST_COLUMN = "first_per_patient"

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

# The dry run reports on every tier. Stratifying on tier C would redraw the
# split, and the counts are still worth printing. The seven compartment
# questions outside a FAST window are probe targets in `ifast`, so their
# positives per split decide whether the probe's macro is measurable. Reading
# them also quiets the "out of step" warning, which fired on 202,896 tier C
# answer rows that the question set asks for by design.
REPORTED_TIERS = ("A", "B", "C")

DEFAULT_LOCK = "../rate/config/modalities/fast_abdomen_ct.lock.json"
DEFAULT_VAL_EXAMS = 1000

# `air_split` drew the frozen split with this seed on 2026-11-25. It stays the
# default so that a re-run reproduces that split instead of drawing a new one.
# Anyone can then regenerate the frozen file and compare it.
DEFAULT_SEED = 20261125

# The prefix a year takes among the stratification labels, so a year cannot
# collide with a finding's name
YEAR_LABEL = "year:"

# The same for the dictation template. The template is nearly an era -- the
# 2026-11-18 extraction counts template A alone from 2019 to 2021 and both from
# 2022 on -- so the year label balances it before 2022 and leaves the later
# years to chance. The template needs an axis of its own because it moves a
# label's prevalence: over that run `ascites` answers Yes on 11.8% of template A
# reports against 8.0% of template B, and `liver_laceration` on 1.2% against
# 0.55%, and both gaps survive conditioning on the trauma indication. A draw
# unbalanced on this axis would bias those two labels, and they are the two
# closest to the project's question.
TEMPLATE_LABEL = "template:"


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


def included_findings(lock_path: str | Path, tiers=INCLUDED_TIERS) -> dict[str, int]:
    """Read which findings the question set includes, and how many wordings each has.

    This mapped a finding to the md5 of each of its wordings until 2026-11-14,
    because `read_answers` keyed its answers that way. It keys them by finding
    id now, so the hashes are no longer the way in and the count is what is
    left worth knowing.

    Parameters
    ----------
    lock_path : str or Path
        The lock file `rate`'s generated question set has beside it.
    tiers : tuple of str, optional
        The tiers to keep.

    Returns
    -------
    dict
        Finding id to its wording count.
    """
    lock = json.loads(Path(lock_path).read_text())
    findings = {}
    for finding, entry in lock["findings"].items():
        if entry.get("tier") in tiers:
            findings[finding] = len(entry["variants"])
    return findings


def finding_positives(
    answers: dict[str, dict[str, dict[int, int]]], findings: dict[str, int]
) -> dict[str, set[str]]:
    """The findings each report is positive for.

    A finding is positive when any of its wordings was answered Yes, which is
    the question set's `max` rule.

    Parameters
    ----------
    answers : dict
        Report id to finding id to variant index to 1, 0 or -1, from
        `read_answers`.
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
            for finding in findings
            if 1 in (by_finding.get(finding) or {}).values()
        }
        for report, by_finding in answers.items()
    }


def visit_sort_key(visit: dict[str, str]) -> tuple[str, str]:
    """Order a patient's visits in time, falling back to the accession number.

    The ultrasound timestamp is the clock this cohort was matched on. A visit
    with no timestamp sorts first, and the accession number breaks a tie so the
    order never depends on dictionary insertion.
    """
    return (visit.get("us_date_time") or "", visit["ct_accession_number"])


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
    templates: dict[str, str] | None = None,
    stratify_findings: bool = False,
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
    templates : dict, optional
        CT accession number to the dictation template of its report, from
        `read_answers`. Each one becomes a stratification label, so the
        validation draw gets its share of both templates. Omitted leaves the
        draw stratified on the year alone.
    stratify_findings : bool, optional
        Add each patient's positive findings to its stratification labels.
        Off since 2026-11-25. The comment in the body records the measurement:
        the findings move the compartment macro's detectable difference by
        0.0001, and they would tie the split to a label set that a
        re-extraction can change.

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
        # The findings are off by default. A simulation of 500 draws on the
        # 2026-11-18 extraction measured what they buy. Stratifying on them
        # moves the compartment macro's detectable difference from 0.0254 to
        # 0.0253, which is nothing. It also keeps a rare label's count closer
        # to its average without making that label measurable.
        # `peritoneal_carcinomatosis` draws 5.2 +- 1.1 positives stratified
        # against 5.2 +- 2.8 uniform, and either figure has a 95% interval of
        # about +-0.23. Stratifying on them would also cost a dependency: a
        # split stratified on a label set cannot outlive a re-extraction that
        # moves one. Year and template stay, because both are structural and
        # neither comes from an answer.
        patient_labels = (
            set().union(*(positives[v["ct_accession_number"]] for v in reported))
            if stratify_findings
            else set()
        )
        if years:
            patient_labels.add(f"{YEAR_LABEL}{min(years)}")
        for visit in reported:
            # A patient with visits under both templates takes both labels,
            # which is what iterative stratification over a multi-label set
            # expects.
            template = (templates or {}).get(visit["ct_accession_number"], "")
            if template:
                patient_labels.add(f"{TEMPLATE_LABEL}{template}")
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
        return sorted(visits, key=visit_sort_key)

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
            # The test set follows the patient's year, so this branch covers a
            # test patient's exam whether or not the extraction read its CT
            # report. An exam with no answer row has no label, and an
            # unscorable exam in the sealed set understates every metric over
            # it, so it goes to `unreported` as it does in the other two draws.
            reported = visit["ct_accession_number"] in positives
            out.append(
                {
                    **visit,
                    SPLIT_COLUMN: split if reported else SPLIT_UNREPORTED,
                    ORDER_COLUMN: "",
                }
            )
    return mark_first_per_patient(out, test_year=test_year)


def mark_first_per_patient(
    rows: list[dict[str, str]], test_year: int = TEST_YEAR
) -> list[dict[str, str]]:
    """Mark one exam per patient, which is the exam a download pulls.

    A patient represented by one exam makes every exam an independent unit, so
    a bootstrap over patients and one over exams become the same thing.

    Which exam represents the patient depends on the split. A validation or
    training patient is represented by their earliest visit. A test patient is
    represented by their earliest visit **in the test year**, because the test
    split also holds their earlier visits to keep a patient off both sides of
    the split, and an earlier visit comes from the era the model trains on. A
    patient whose exams have no report is marked nowhere.

    Parameters
    ----------
    rows : list of dict
        Rows that already have `SPLIT_COLUMN`, from `assign_splits`.
    test_year : int, optional
        The year the test split holds out.

    Returns
    -------
    list of dict
        The same rows, each with `FIRST_COLUMN` set to "1" or "0".
    """
    def key(row):
        return (row["mrn"], row["us_accession_number"], row["ct_accession_number"])

    best: dict[str, dict[str, str]] = {}
    for row in rows:
        split = row[SPLIT_COLUMN]
        if split == SPLIT_UNREPORTED:
            continue
        if split == SPLIT_TEST and visit_year(row) != test_year:
            continue
        current = best.get(row["mrn"])
        if current is None or visit_sort_key(row) < visit_sort_key(current):
            best[row["mrn"]] = row

    chosen = {key(row) for row in best.values()}
    for row in rows:
        row[FIRST_COLUMN] = "1" if key(row) in chosen else "0"
    return rows


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
        ``patients``, ``exams``, ``years``, ``unanswered`` and ``findings``,
        each keyed by split. Every value is a count.
    """
    patients: dict[str, set[str]] = defaultdict(set)
    exams: Counter = Counter()
    firsts: Counter = Counter()
    years: dict[str, Counter] = defaultdict(Counter)
    unanswered: Counter = Counter()
    unanswered_firsts: Counter = Counter()
    finding_counts: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        split = row[SPLIT_COLUMN]
        patients[split].add(row["mrn"])
        exams[split] += 1
        marked = row.get(FIRST_COLUMN) == "1"
        if marked:
            firsts[split] += 1
        years[split][visit_year(row)] += 1
        # A CT absent from `positives` produced no answer row, so the
        # extraction never read its report. `assign_splits` files such an exam
        # under `unreported` in the validation and training draws, while test
        # membership follows the patient's year alone. Count it per split, so
        # a download never takes an exam that can have no label.
        if row["ct_accession_number"] not in positives:
            unanswered[split] += 1
            if marked:
                unanswered_firsts[split] += 1
        for finding in positives.get(row["ct_accession_number"], ()):
            finding_counts[split][finding] += 1
    return {
        "patients": {split: len(members) for split, members in patients.items()},
        "exams": dict(exams),
        "firsts": dict(firsts),
        "years": {split: dict(counter) for split, counter in years.items()},
        "unanswered": dict(unanswered),
        "unanswered_firsts": dict(unanswered_firsts),
        "findings": {
            finding: {split: finding_counts[split][finding] for split in SPLITS}
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
    added = (SPLIT_COLUMN, ORDER_COLUMN, FIRST_COLUMN)
    columns = [column for column in rows[0] if column not in added]
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*columns, *added])
        writer.writeheader()
        writer.writerows(rows)
    return output


def log_counts(counts: dict) -> None:
    """Log what `split_counts` returned. Every value is a count."""
    for split in SPLITS:
        # One exam per patient is marked, so the last two columns agree unless
        # something has gone wrong in the marking.
        logger.info(
            "%-10s %6d patient(s), %6d exam(s), %6d marked first per patient",
            split,
            counts["patients"].get(split, 0),
            counts["exams"].get(split, 0),
            counts["firsts"].get(split, 0),
        )
    for split in SPLITS:
        missing = counts.get("unanswered", {}).get(split, 0)
        if missing:
            logger.warning(
                "%-10s %6d exam(s) whose CT report the extraction never read, "
                "%d of them marked first per patient. Such an exam has no "
                "label, so leave it out of a download.",
                split,
                missing,
                counts.get("unanswered_firsts", {}).get(split, 0),
            )
    all_years = sorted({year for by_year in counts["years"].values() for year in by_year if year})
    logger.info("Exams per year, val / train / test:")
    for year in all_years:
        logger.info(
            "  %d  %5d / %5d / %5d",
            year,
            *(counts["years"].get(split, {}).get(year, 0) for split in (SPLIT_VAL, SPLIT_TRAIN, SPLIT_TEST)),
        )
    logger.info("Positive exams per finding, val / train / test:")
    for finding, by_split in sorted(counts["findings"].items()):
        logger.info(
            "  %-55s %4d / %5d / %4d",
            finding,
            by_split[SPLIT_VAL],
            by_split[SPLIT_TRAIN],
            by_split[SPLIT_TEST],
        )


def main(
    matched_csv: str,
    answers: str,
    lock: str = DEFAULT_LOCK,
    output: str | None = None,
    val_exams: int = DEFAULT_VAL_EXAMS,
    seed: int = DEFAULT_SEED,
    test_year: int = TEST_YEAR,
    stratify_findings: bool = False,
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
    stratify_findings : bool, optional
        Add the findings to the stratification labels. Off by default, and
        `assign_splits` records the measurement behind that.
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
    reported = included_findings(lock, tiers=REPORTED_TIERS)
    wordings = sum(findings.values())
    logger.info(
        "%d included finding(s) over %d wording(s); %d reported.",
        len(findings),
        wordings,
        len(reported),
    )

    rows = read_matched_rows(matched)
    wanted = {row["ct_accession_number"] for row in rows}
    found, templates, _levels = read_answers(answers, wanted, set(reported))
    positives = finding_positives(found, reported)

    split = assign_splits(
        rows,
        # The draw stratifies on the included tiers alone, so widening the
        # report above cannot move it.
        finding_positives(found, findings),
        val_exams=val_exams,
        seed=seed,
        test_year=test_year,
        templates=templates,
        stratify_findings=stratify_findings,
    )
    log_counts(split_counts(split, positives, reported))
    logger.info(
        "Seed %d, test year %d, %d validation exam(s) requested, "
        "stratified on year and template%s.",
        seed,
        test_year,
        val_exams,
        " and the findings" if stratify_findings else "",
    )

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
