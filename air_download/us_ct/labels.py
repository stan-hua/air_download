"""
labels.py

Description: Write the CT findings of a downloaded cohort, keyed by pseudonym
             rather than by accession number.

The sibling `rate` project answers a fixed set of yes/no questions against each
CT report and writes `questions.csv`, keyed by `report_id`. That identifier is
the CT's real accession number, so the file cannot be joined to a cohort
anywhere but here: the crosswalk is the only thing that maps an accession to a
`P0001`/`A0001`, and it never leaves this project.

So this does the join and throws the key away. One row per ultrasound-CT pair,
keyed by `anon_mrn` and `anon_accession_number`, one column per question,
holding 1 for Yes, 0 for No and -1 where the extractor returned no verdict. The
modelling project reads that and nothing else.

Every question RATE asked gets a column, not the subset a FAST window could
show. Which findings are worth predicting is a modelling decision argued in
`ifast/src/ct_findings/findings.py`, and changing it must not need the
crosswalk, this script, or a rerun of the extraction.

Nothing here logs a row. Counts alone.

Examples
--------
Write the labels for a downloaded cohort::

    pixi run labels --answers ../ifast/data/metadata/fast_ct_cohort/questions.csv \
                    --crosswalk tmp/cohort-test25_crosswalk.csv \
                    --output ../ifast/data/metadata/labels

Check the join without writing anything::

    pixi run labels --answers questions.csv --crosswalk out_crosswalk.csv --dry_run
"""

# Standard libraries
import csv
import hashlib
import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Non-standard libraries
import fire
import yaml

# Custom libraries
from air_download.crosswalk import CROSSWALK_CSV_HEADER
from air_download.utils import as_identifier, parse_datetime

logger = logging.getLogger(__name__)

# What `air_convert` names an ultrasound and a CT half of a visit
EXAM_TYPES = ("us", "ct")

# One column per question, named for RATE's own hash of the question text. A
# reader-facing name would stop identifying the question the moment somebody
# reworded it, and it would be unusable as a CSV header.
QUESTION_PREFIX = "q_"

# What a cell holds where no line of the answer carried a verdict. It stays a
# third state rather than becoming a No, because a question nobody answered is
# not a negative finding.
UNANSWERED = -1

# Everything about the exam pair that is not a label. `anon_accession_number`
# is the ultrasound's, because that is what the modelling project reads off the
# array path and joins every other table on.
KEY_COLUMNS = (
    "anon_mrn",
    "anon_accession_number",
    "anon_ct_accession_number",
    "visit",
    "us_ct_delta_minutes",
    "downloaded",
    "label_set_version",
)


def question_id(question: str) -> str:
    """Return the identifier RATE gives a question.

    Reimplements `StorageManager._generate_question_id` in `../rate`, which
    sits behind a served model this project cannot install. `ifast` carries a
    third copy for the same reason; all three have to agree, because the hash
    is what names the column.

    Parameters
    ----------
    question : str
        The question text, worded as the modality config words it.

    Returns
    -------
    str
        Eight hex characters.
    """
    return hashlib.md5(question.encode("utf-8")).hexdigest()[:8]


def parse_answer(answer: str) -> int:
    """Read a Yes/No verdict out of RATE's free-text answer.

    Reimplements `parse_answer` in `ifast/src/ct_findings/findings.py`, whose
    docstring records what the looser answers look like: a trailing period, a
    repeated verdict on its own line, a paragraph of reasoning that opens with
    "No", and one unclosed `<think>` block carrying no verdict at all. The
    first line that opens with yes or no decides.

    Parameters
    ----------
    answer : str
        The `answer` field as RATE wrote it.

    Returns
    -------
    int
        1, 0, or UNANSWERED.
    """
    for line in str(answer).splitlines():
        match = re.match(r"^\s*(yes|no)\b", line, re.IGNORECASE)
        if match:
            return 1 if match.group(1).lower() == "yes" else 0
    return UNANSWERED


def read_questions(modality_config: str | Path) -> dict[str, dict[str, str]]:
    """Read the question set RATE asked, from its modality config.

    Parameters
    ----------
    modality_config : str or Path
        A YAML file under `rate/config/modalities/`.

    Returns
    -------
    dict
        Question id to its text and the category it was asked under. A
        question asked under two categories keeps the first.
    """
    config = yaml.safe_load(Path(modality_config).read_text())
    questions: dict[str, dict[str, str]] = {}
    for category, entry in (config.get("categories") or {}).items():
        for item in entry.get("questions") or []:
            text = item["question"]
            questions.setdefault(
                question_id(text), {"question": text, "category": category}
            )
    return questions


def config_digest(modality_config: str | Path) -> str:
    """Return the question set's version, as a short digest of its config."""
    digest = hashlib.sha256(Path(modality_config).read_bytes()).hexdigest()[:8]
    return f"{Path(modality_config).stem}@{digest}"


def read_pairs(crosswalk: str | Path) -> list[dict[str, str]]:
    """Reconstruct each ultrasound-CT pair from the crosswalk.

    A visit's two rows share `anon_mrn` and `visit_folder` and differ in
    `exam_type`, so the pairing needs no match list. A visit missing either
    half is dropped with a warning, because half a pair cannot be a row.

    Parameters
    ----------
    crosswalk : str or Path
        The `<output>_crosswalk.csv` a download wrote.

    Returns
    -------
    list of dict
        One entry per complete pair, carrying both pseudonyms, the visit, the
        CT's real accession number and the two timestamps.

    Raises
    ------
    FileNotFoundError
        If the crosswalk is missing.
    ValueError
        If it is missing a required column.
    """
    path = Path(crosswalk)
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist.")

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in CROSSWALK_CSV_HEADER if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} is missing required column(s): {', '.join(missing)}")
        rows = [dict(row) for row in reader]

    visits: dict[tuple[str, str], dict[str, dict[str, str]]] = defaultdict(dict)
    for row in rows:
        exam_type = (row.get("exam_type") or "").strip().lower()
        if exam_type in EXAM_TYPES:
            visits[(row["anon_mrn"], row["visit_folder"])][exam_type] = row

    pairs, incomplete = [], 0
    for (anon_mrn, visit), halves in sorted(visits.items()):
        if not all(half in halves for half in EXAM_TYPES):
            incomplete += 1
            continue
        us, ct = halves["us"], halves["ct"]
        pairs.append(
            {
                "anon_mrn": anon_mrn,
                "visit": visit,
                "anon_accession_number": us["anon_accession_number"],
                "anon_ct_accession_number": ct["anon_accession_number"],
                "ct_accession_number": as_identifier(ct.get("accession_number")),
                "us_date_time": us.get("date_time", ""),
                "ct_date_time": ct.get("date_time", ""),
                "archive_path": us.get("archive_path", ""),
            }
        )

    if incomplete:
        logger.warning("%d visit(s) hold only one half of a pair and were dropped.", incomplete)
    logger.info("Reconstructed %d ultrasound-CT pair(s) from the crosswalk.", len(pairs))
    return pairs


def delta_minutes(start: str, end: str) -> int | str:
    """Return the minutes between two crosswalk timestamps, or "" if unreadable.

    The interval survives into the label table; neither timestamp does. A
    date and time of care is a quasi-identifier, and the gap between the two
    exams is the only part of it a model has any use for.
    """
    try:
        first, second = parse_datetime(start), parse_datetime(end)
    except (ValueError, TypeError):
        return ""
    return int(round((second - first).total_seconds() / 60.0))


def read_answers(
    answers: str | Path, wanted: set[str], questions: set[str]
) -> dict[str, dict[str, int]]:
    """Read the answers belonging to a cohort's CT reports.

    Streamed rather than loaded: the file runs to millions of rows, and all but
    a few thousand belong to reports this cohort never downloaded.

    Parameters
    ----------
    answers : str or Path
        RATE's `questions.csv`.
    wanted : set of str
        The report identifiers to keep, which are CT accession numbers.
    questions : set of str
        The question ids to keep.

    Returns
    -------
    dict
        Report identifier to a mapping of question id to 1, 0 or UNANSWERED. A
        Yes wins over a No for the one question filed under two categories.
    """
    path = Path(answers)
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist.")

    csv.field_size_limit(10**9)
    found: dict[str, dict[str, int]] = defaultdict(dict)
    unknown_question, conflicts = 0, 0

    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            report = (row.get("report_id") or "").strip()
            if report not in wanted:
                continue
            question = (row.get("question_id") or "").strip()
            if question not in questions:
                unknown_question += 1
                continue
            label = parse_answer(row.get("answer"))
            previous = found[report].get(question)
            if previous is None:
                found[report][question] = label
            elif previous != label:
                # One question is filed under two categories and answered
                # twice. A Yes wins, as it does everywhere else.
                conflicts += 1
                found[report][question] = max(previous, label)

    if unknown_question:
        logger.warning(
            "%d answer row(s) name a question the modality config does not, and were "
            "skipped. The config and the extraction run are out of step.",
            unknown_question,
        )
    if conflicts:
        logger.info("%d cell(s) were answered twice and disagreed; a Yes won.", conflicts)
    logger.info("Read answers for %d of %d report(s) in the cohort.", len(found), len(wanted))
    return found


def build_rows(pairs, answers, questions, version, arrays=None):
    """Assemble one row per exam pair.

    Parameters
    ----------
    pairs : list of dict
        What `read_pairs` returned.
    answers : dict
        What `read_answers` returned.
    questions : dict
        What `read_questions` returned. Column order follows its key order.
    version : str
        The question set's version, from `config_digest`.
    arrays : str or Path, optional
        The converted cohort, used to fill `downloaded`. Absent means every
        pair is marked downloaded, which is what a crosswalk of a completed
        download already implies.

    Returns
    -------
    tuple of (list of dict, int)
        The rows, and how many pairs had no CT report at all.
    """
    columns = [f"{QUESTION_PREFIX}{question}" for question in questions]
    rows, unreported = [], 0

    for pair in pairs:
        found = answers.get(pair["ct_accession_number"])
        if found is None:
            unreported += 1
            found = {}

        if arrays is None:
            downloaded = True
        else:
            group = Path(arrays) / pair["anon_mrn"] / pair["visit"] / "us"
            downloaded = (group / f"{pair['anon_accession_number']}.zarr").exists()

        row = {
            "anon_mrn": pair["anon_mrn"],
            "anon_accession_number": pair["anon_accession_number"],
            "anon_ct_accession_number": pair["anon_ct_accession_number"],
            "visit": pair["visit"],
            "us_ct_delta_minutes": delta_minutes(pair["us_date_time"], pair["ct_date_time"]),
            "downloaded": downloaded,
            "label_set_version": version,
        }
        for column, question in zip(columns, questions, strict=True):
            row[column] = found.get(question, UNANSWERED)
        rows.append(row)

    if unreported:
        logger.warning(
            "%d pair(s) have no CT report in the answers and carry no label at all. "
            "They are kept, so the modelling project can see the gap.",
            unreported,
        )
    return rows, unreported


def write_labels(rows, questions, output, version):
    """Write the table and the sidecar that says what its columns mean.

    Parameters
    ----------
    rows : list of dict
        What `build_rows` returned.
    questions : dict
        What `read_questions` returned.
    output : str or Path
        The directory both files go in.
    version : str
        The question set's version.

    Returns
    -------
    tuple of (Path, Path)
        The table and the sidecar.
    """
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    table = directory / "exam_findings.csv"
    sidecar = directory / "exam_findings.json"

    header = list(KEY_COLUMNS) + [f"{QUESTION_PREFIX}{question}" for question in questions]
    with table.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)

    sidecar.write_text(
        json.dumps(
            {
                "written_at": datetime.now().isoformat(timespec="seconds"),
                "writer": "air_labels",
                "label_set_version": version,
                "n_exams": len(rows),
                "n_questions": len(questions),
                "questions": questions,
            },
            indent=2,
        )
    )
    return table, sidecar


def main(
    answers: str,
    crosswalk: str,
    modality_config: str = "../rate/config/modalities/abdomen_ct.yaml",
    output: str = "../ifast/data/metadata/labels",
    arrays: str | None = None,
    dry_run: bool = False,
) -> None:
    """Join a cohort's CT report answers to its pseudonyms, and write them out.

    Parameters
    ----------
    answers : str
        RATE's `questions.csv`, keyed by `report_id`.
    crosswalk : str
        The `<output>_crosswalk.csv` the download wrote.
    modality_config : str, optional
        The modality YAML RATE asked its questions from.
    output : str, optional
        Directory for `exam_findings.csv` and its sidecar.
    arrays : str, optional
        The converted cohort, used to fill the `downloaded` column.
    dry_run : bool, optional
        Report the join and write nothing.

    Raises
    ------
    FileNotFoundError
        If any input is missing.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    questions = read_questions(modality_config)
    version = config_digest(modality_config)
    logger.info("The question set holds %d question(s), version %s.", len(questions), version)

    pairs = read_pairs(crosswalk)
    wanted = {pair["ct_accession_number"] for pair in pairs if pair["ct_accession_number"]}
    found = read_answers(answers, wanted, set(questions))

    rows, unreported = build_rows(pairs, found, questions, version, arrays)
    positives = sum(
        1 for row in rows if any(row[key] == 1 for key in row if key.startswith(QUESTION_PREFIX))
    )
    logger.info(
        "%d exam pair(s), %d with at least one Yes, %d with no report.",
        len(rows),
        positives,
        unreported,
    )

    if dry_run:
        logger.info("Dry run: nothing written.")
        return

    table, sidecar = write_labels(rows, questions, output, version)
    logger.info("Wrote %s and %s.", table, sidecar)


def cli() -> None:
    """Entry point for `air_labels`."""
    fire.Fire(main)


if __name__ == "__main__":
    cli()
