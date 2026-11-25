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
keyed by `anon_mrn` and `anon_accession_number`, with three columns per
finding: `q_` holds 1 for Yes, 0 for No and -1 where no wording returned a
verdict, `nvote_` how many wordings answered Yes, and `disc_` whether a Yes
rests on fewer wordings than the set asks. The modelling project reads that and
nothing else.

`q_` is the **vote**, `max` over the wordings asked, and there is no second
column holding the canonical answer alone. The two readings were reported side
by side in an earlier draft of this design and that was rejected: one number
per finding, with the disagreement beside it as a flag.

A long-form `exam_findings_variants.csv` carries each wording's own answer, so
nothing in the wide table has to grow a column per wording.

Every finding RATE asked about gets its columns, not the subset a FAST window
could show. Which findings are worth predicting is a modelling decision argued
in `ifast/src/ct_findings/findings.py`, and changing it must not need the
crosswalk, this script, or a rerun of the extraction.

Nothing here logs a row. Counts alone.

Examples
--------
Write the labels for a downloaded cohort::

    pixi run labels --answers ../ifast/data/fast_ct_cohort/metadata/ct_text_reports/questions.csv \
                    --crosswalk tmp/cohort-test25_crosswalk.csv \
                    --output ../ifast/data/fast_ct_25/metadata/labels

Add each pair's split and training order, from the file `air_split` wrote::

    pixi run labels --answers questions.csv --crosswalk out_crosswalk.csv \
                    --splits matched_us_ct-2019_2026-split.csv \
                    --output ../ifast/data/fast_ct_train/metadata/labels

Check the join without writing anything::

    pixi run labels --answers questions.csv --crosswalk out_crosswalk.csv --dry_run
"""

# Standard libraries
import csv
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
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

# Three columns per finding, named for the authored finding id.
#
# `q_` was named for RATE's md5 of the question text until 2026-11-14, when the
# re-key that `ifast` made in Phase 2 finally reached the module that writes the
# table. A hash stops identifying anything the moment somebody rewords the
# question, and the question set now asks each finding under two or three
# wordings, so a hash-named column would be one column per *wording* with no
# rule for combining them.
#
# `q_` holds the vote, `nvote_` how many wordings answered Yes, and `disc_`
# whether they disagreed. The vote is the label and the other two are what make
# training on the agreed rows a decision rather than a default: the third pilot
# had 1,263 discordant cells in 16,713 positive ones, 7.6%.
QUESTION_PREFIX = "q_"
VOTE_COUNT_PREFIX = "nvote_"
DISCORD_PREFIX = "disc_"

# What a cell holds where no wording of the finding carried a verdict. It stays
# a third state rather than becoming a No, because a question nobody answered
# is not a negative finding.
UNANSWERED = -1

# What a stored `verdict` means as a label. `not_assessed` is deliberately
# absent and becomes `UNANSWERED`: the report states it cannot answer, and
# folding that into a No would make `rate`'s deferral rule a no-op, since that
# rule only ever moves a No. Reimplements `VERDICT_LABELS` in
# `ifast/src/ct_findings/findings.py`.
VERDICT_LABELS = {"yes": 1, "no": 0}

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
    # The CT report's dictation template, which `rate` reads off its section
    # headings. A15 measured two templates dividing the cohort 7,946 to 4,721,
    # differing in 14 headings and in which compartments appear at all --
    # urinary tract in 9.5% of one against 99.5% of the other. If those are two
    # eras they confound every label here, and the folds cannot stratify on a
    # column the table does not have.
    "template",
    # Whether the CT report's indication describes a traumatic presentation,
    # and which categories named it. This cohort pairs an ED FAST with a CT
    # and a FAST is a trauma protocol, so the flag splits it into the two
    # groups every claim about what a sweep misses is read against. Measured
    # over the 12,681 reports: 6,186 yes, 6,459 no, 36 with no indication
    # section, and a flagged report carries an acute traumatic finding 25.9%
    # of the time against 7.6% for an unflagged one.
    "trauma_indication",
    "trauma_terms",
    "label_set_version",
    # A digest of the decode configuration the answers were produced under.
    # A17 asked for this and nothing carried it: the labels now depend on
    # thinking being on, `run_guard`'s manifest records that inside the save
    # directory, and the save directory is not what the modelling project
    # reads.
    "decode_version",
    "split",
    "train_order",
    # Whether this is the one pair the cohort keeps for this patient. A
    # download pulls the marked pairs, so an exam is an independent unit and
    # no figure has to be averaged within a patient first.
    # `ifast/docs/PRETRAINING.md` states the rule and what it costs.
    "first_per_patient",
)


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


def read_questions(modality_config: str | Path) -> dict[str, dict]:
    """Read the findings RATE asked about, from its modality config.

    Keyed by the authored finding id. Each entry also records how many wordings
    the finding is asked under, because a vote of one Yes means something
    different at two wordings and at three, and the count is what tells them
    apart without a fourth column per finding.

    Parameters
    ----------
    modality_config : str or Path
        A YAML file under `rate/config/modalities/`.

    Returns
    -------
    dict
        Finding id to its canonical wording, its category and its wording
        count. A finding filed under two categories keeps the first.

    Raises
    ------
    ValueError
        When an entry has no `id`, which means a config predating the authored
        finding ids. The answers for such a run are keyed by a wording hash and
        no vote can be taken over them.
    """
    config = yaml.safe_load(Path(modality_config).read_text())
    questions: dict[str, dict] = {}
    unidentified = []
    for category, entry in (config.get("categories") or {}).items():
        for item in entry.get("questions") or []:
            identifier = (item.get("id") or "").strip()
            if not identifier:
                unidentified.append(item.get("question", "")[:40])
                continue
            questions.setdefault(
                identifier,
                {
                    "question": item["question"],
                    "category": category,
                    "n_wordings": 1 + len(item.get("variants") or []),
                },
            )
    if unidentified:
        raise ValueError(
            f"{len(unidentified)} question(s) in {modality_config} have no `id`, so this "
            "config predates the authored finding ids and its answers cannot be voted "
            "over. Regenerate it with ifast's `build-rate-config`."
        )
    return questions


def vote(by_variant: dict[int, int], n_wordings: int) -> tuple[int, int, int]:
    """Combine a finding's wordings into one label, under `max`.

    The ADR settles `max` over a majority. Here it means a Yes under any
    wording is a Yes, and `UNANSWERED` is ignored unless every wording returned
    it -- a finding two wordings answered No and a third left blank is a No,
    not an unknown.

    Parameters
    ----------
    by_variant : dict
        Variant index to 1, 0 or `UNANSWERED`.
    n_wordings : int
        How many wordings the config asks this finding under, which decides
        whether a partial answer is discordant or merely incomplete.

    Returns
    -------
    tuple of (int, int, int)
        The label, how many wordings answered Yes, and 1 where the wordings
        that answered disagreed.
    """
    answered = [value for value in by_variant.values() if value != UNANSWERED]
    if not answered:
        return UNANSWERED, 0, 0
    yes = sum(1 for value in answered if value == 1)
    label = 1 if yes else 0
    # Discordant where the answers differ, and where a wording went missing
    # while the others said Yes: a Yes resting on fewer wordings than the set
    # asks is the stratum A30 routes into these columns.
    discordant = int(0 < yes < n_wordings)
    return label, yes, discordant


def config_digest(modality_config: str | Path) -> str:
    """Return the question set's version, for `label_set_version`.

    A lock file beside the config records a `question_set` digest over the
    wordings alone, and that is the right version to stamp a label table with:
    it moves when a question moves and stays put when a definition, a tier or a
    category is edited, none of which changes what a stored answer means. A
    digest of the file's bytes moves on all of those, so a table would look
    stale after a comment was reflowed.

    Falls back to the file digest when no lock sits beside the config, which is
    true of every modality config except the generated FAST set.

    Parameters
    ----------
    modality_config : str or Path
        A YAML file under `rate/config/modalities/`.

    Returns
    -------
    str
        `<config stem>@<digest>`.
    """
    path = Path(modality_config)
    lock = path.with_suffix(".lock.json")
    if lock.exists():
        recorded = json.loads(lock.read_text()).get("digest", {}).get("question_set")
        if recorded:
            return f"{path.stem}@{recorded}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    return f"{path.stem}@{digest}"


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
                "mrn": as_identifier(us.get("mrn")),
                "us_accession_number": as_identifier(us.get("accession_number")),
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
    answers: str | Path, wanted: set[str], findings: set[str]
) -> tuple[dict[str, dict[str, dict[int, int]]], dict[str, str], dict[tuple, tuple]]:
    """Read the answers belonging to a cohort's CT reports.

    Streamed rather than loaded: the file runs to three million rows on the
    full cohort, and all but a few thousand belong to reports this cohort never
    downloaded.

    Every wording is kept separately, keyed by its `variant_index`, because the
    vote is taken here and the per-wording answers go into the long-form
    sidecar. `verdict` is read in preference to `answer`, since `rate` has
    already parsed it; `parse_answer` is the fallback for a file written before
    that column existed.

    Parameters
    ----------
    answers : str or Path
        RATE's `questions.csv`.
    wanted : set of str
        The report identifiers to keep, which are CT accession numbers.
    findings : set of str
        The finding ids to keep.

    The mapped `certainty` and `severity` come back too, keyed per wording,
    because the severity threshold is applied when a claim is made rather than
    baked into the label. Only the mapped levels travel: `certainty_phrase`,
    `severity_phrase` and `evidence` are verbatim report sentences and go no
    further than this file.

    Returns
    -------
    tuple of (dict, dict, dict)
        Report identifier to finding id to variant index to 1, 0 or
        `UNANSWERED`; report identifier to its covariates, each column name
        to its value; and `(report, finding, index)` to its
        `(certainty, severity)`.

    Raises
    ------
    ValueError
        When the file has no `finding_id` column, which means an extraction
        predating the re-key.
    """
    path = Path(answers)
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist.")

    csv.field_size_limit(10**9)
    found: dict[str, dict[str, dict[int, int]]] = defaultdict(lambda: defaultdict(dict))
    # One entry per report, holding whatever covariates the row carries. A
    # second covariate arrived with the trauma flag, and a dict per report
    # takes a third without changing this function's shape again.
    covariates: dict[str, dict[str, str]] = {}
    levels: dict[tuple, tuple] = {}
    unknown_finding, conflicts, repeats = 0, 0, 0

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames and "finding_id" not in reader.fieldnames:
            raise ValueError(
                f"{path} has no `finding_id` column, so it predates the authored finding "
                "ids and its columns are named for a wording hash. Re-run the extraction "
                "against the generated FAST config."
            )
        for row in reader:
            report = (row.get("report_id") or "").strip()
            if report not in wanted:
                continue
            finding = (row.get("finding_id") or "").strip()
            if finding not in findings:
                unknown_finding += 1
                continue

            verdict = (row.get("verdict") or "").strip().lower()
            if verdict:
                label = VERDICT_LABELS.get(verdict, UNANSWERED)
            else:
                label = parse_answer(row.get("answer"))

            index = (row.get("variant_index") or "").strip()
            index = int(index) if index.isdigit() else 0

            previous = found[report][finding].get(index)
            if previous is None:
                found[report][finding][index] = label
            elif previous != label:
                # One finding filed under two categories and answered twice
                # under the same wording. A Yes wins, as it does everywhere.
                conflicts += 1
                found[report][finding][index] = max(previous, label)
            else:
                repeats += 1

            levels[(report, finding, index)] = (
                (row.get("certainty") or "").strip(),
                (row.get("severity") or "").strip(),
            )

            for name in ("template", "trauma_indication", "trauma_terms"):
                value = (row.get(name) or "").strip()
                if value:
                    covariates.setdefault(report, {}).setdefault(name, value)

    if unknown_finding:
        logger.warning(
            "%d answer row(s) name a finding the modality config does not, and were "
            "skipped. The config and the extraction run are out of step.",
            unknown_finding,
        )
    if conflicts:
        logger.info("%d cell(s) were answered twice and disagreed; a Yes won.", conflicts)
    if repeats:
        logger.info("%d cell(s) were answered twice and agreed.", repeats)
    logger.info("Read answers for %d of %d report(s) in the cohort.", len(found), len(wanted))
    if covariates:
        tally = Counter(e.get("template", "") for e in covariates.values())
        logger.info(
            "Dictation template: %s",
            ", ".join(f"{name or 'absent'} {count}" for name, count in sorted(tally.items())),
        )
        trauma = Counter(e.get("trauma_indication", "") for e in covariates.values())
        logger.info(
            "Trauma indication: %s",
            ", ".join(f"{name or 'absent'} {count}" for name, count in sorted(trauma.items())),
        )
    else:
        logger.warning(
            "No answer row carries a `template`, so the folds cannot stratify on it. "
            "The extraction predates rate's template detection."
        )
    return found, covariates, levels


def decode_digest(answers: str | Path) -> tuple[str, dict]:
    """Return the decode configuration the answers were produced under.

    `run_guard` writes `run_manifest.json` into the save directory on the first
    batch, recording the model name, the sampling block, `max_tokens`,
    `enable_thinking` and `structured_output`. Those settle what a wording
    answers as surely as the wording does -- `aortic_atherosclerosis` answered
    4/3/6 Yes under its three wordings and then 1/2/5 with no wording changed,
    because the sampler had moved -- and nothing downstream of the save
    directory recorded them.

    Parameters
    ----------
    answers : str or Path
        RATE's `questions.csv`. The manifest is looked for beside it.

    Returns
    -------
    tuple of (str, dict)
        An eight-character digest and the configuration it covers. The digest
        is empty where no manifest sits beside the answers.
    """
    manifest = Path(answers).parent / "run_manifest.json"
    if not manifest.exists():
        logger.warning(
            "No run_manifest.json beside %s, so the table records no decode "
            "configuration. The answers were produced before the resume guard existed.",
            answers,
        )
        return "", {}
    decode = json.loads(manifest.read_text()).get("decode") or {}
    if not decode:
        return "", {}
    canonical = json.dumps(decode, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8], decode


def read_splits(path: str | Path) -> dict[tuple[str, str, str], tuple[str, str, str]]:
    """Read the split, training order and per-patient mark `air_split` gave each pair.

    Parameters
    ----------
    path : str or Path
        The split match list. It has real identifiers, so nothing here logs a
        row of it.

    Returns
    -------
    dict
        ``(mrn, us_accession_number, ct_accession_number)`` to
        ``(split, train_order, first_per_patient)``.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist.")
    columns = (
        "mrn",
        "us_accession_number",
        "ct_accession_number",
        "split",
        "train_order",
        "first_per_patient",
    )
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} is missing required column(s): {', '.join(missing)}")
        return {
            tuple(as_identifier(row[c]) for c in columns[:3]): (
                (row["split"] or "").strip(),
                (row["train_order"] or "").strip(),
                (row["first_per_patient"] or "").strip(),
            )
            for row in reader
        }


def build_rows(
    pairs,
    answers,
    questions,
    version,
    arrays=None,
    splits=None,
    covariates=None,
    decode_version="",
    levels=None,
):
    """Assemble one row per exam pair, and one long-form row per wording.

    The wide table has three columns per finding and the long-form table one
    row per pair, finding and wording. Step 25 put the per-wording answers in a
    sidecar rather than in the wide table, where they would have been 243
    further columns.

    **The long-form table holds the mapped levels and no report text.**
    `certainty` and `severity` travel because the severity threshold is applied
    when a claim is made rather than baked into the label -- `ascites` agrees
    at J = 0.944 on any Yes and 0.987 on moderate-or-marked, and 77% of its
    positives are trace or mild. `certainty_phrase`, `severity_phrase` and
    `evidence` are verbatim quotes from the report and stay out of both files.

    Parameters
    ----------
    pairs : list of dict
        What `read_pairs` returned.
    answers : dict
        What `read_answers` returned, first element.
    questions : dict
        What `read_questions` returned. Column order follows its key order.
    version : str
        The question set's version, from `config_digest`.
    arrays : str or Path, optional
        The converted cohort, used to fill `downloaded`. Absent means every
        pair is marked downloaded, which is what a crosswalk of a completed
        download already implies.
    splits : dict, optional
        What `read_splits` returned. Absent leaves `split` and `train_order`
        empty.
    covariates : dict, optional
        What `read_answers` returned, second element. Absent leaves
        `template` and the two trauma columns empty.
    decode_version : str, optional
        What `decode_digest` returned.
    levels : dict, optional
        What `read_answers` returned, third element. Absent leaves `certainty`
        and `severity` empty in the long-form table.

    Returns
    -------
    tuple of (list of dict, list of dict, int)
        The wide rows, the long-form rows, and how many pairs had no CT report
        at all.
    """
    covariates = covariates or {}
    levels = levels or {}
    rows, variants, unreported = [], [], 0

    for pair in pairs:
        accession = pair["ct_accession_number"]
        found = answers.get(accession)
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
            **{
                name: (covariates.get(accession) or {}).get(name, "")
                for name in ("template", "trauma_indication", "trauma_terms")
            },
            "label_set_version": version,
            "decode_version": decode_version,
        }
        for finding, entry in questions.items():
            by_variant = found.get(finding) or {}
            label, yes, discordant = vote(by_variant, entry["n_wordings"])
            row[f"{QUESTION_PREFIX}{finding}"] = label
            row[f"{VOTE_COUNT_PREFIX}{finding}"] = yes
            row[f"{DISCORD_PREFIX}{finding}"] = discordant
            for index in sorted(by_variant):
                certainty, severity = levels.get((accession, finding, index), ("", ""))
                variants.append(
                    {
                        "anon_mrn": pair["anon_mrn"],
                        "anon_accession_number": pair["anon_accession_number"],
                        "finding_id": finding,
                        "variant_index": index,
                        "label": by_variant[index],
                        "certainty": certainty,
                        "severity": severity,
                    }
                )
        split, order, first = ("", "", "")
        if splits is not None:
            key = (pair["mrn"], pair["us_accession_number"], pair["ct_accession_number"])
            split, order, first = splits.get(key, ("", "", ""))
        row["split"], row["train_order"] = split, order
        row["first_per_patient"] = first
        rows.append(row)

    if splits is not None:
        unsplit = sum(1 for row in rows if not row["split"])
        if unsplit:
            logger.warning("%d pair(s) are not in the split file and have no split.", unsplit)

    if unreported:
        logger.warning(
            "%d pair(s) have no CT report in the answers and carry no label at all. "
            "They are kept, so the modelling project can see the gap.",
            unreported,
        )

    discordant_cells = sum(
        value
        for row in rows
        for key, value in row.items()
        if key.startswith(DISCORD_PREFIX)
    )
    positive_cells = sum(
        1
        for row in rows
        for key, value in row.items()
        if key.startswith(QUESTION_PREFIX) and value == 1
    )
    logger.info(
        "%d positive cell(s), of which %d rest on fewer wordings than the set asks.",
        positive_cells,
        discordant_cells,
    )
    return rows, variants, unreported


def write_labels(rows, questions, output, version, variants=None, decode=None):
    """Write the table, the long-form sidecar and the JSON that explains them.

    Parameters
    ----------
    rows : list of dict
        The wide rows `build_rows` returned.
    questions : dict
        What `read_questions` returned.
    output : str or Path
        The directory the files go in.
    version : str
        The question set's version.
    variants : list of dict, optional
        The long-form rows `build_rows` returned. Absent writes no sidecar.
    decode : dict, optional
        The decode configuration `decode_digest` returned.

    Returns
    -------
    tuple of (Path, Path, Path or None)
        The table, the JSON sidecar, and the long-form table.
    """
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    table = directory / "exam_findings.csv"
    sidecar = directory / "exam_findings.json"

    header = list(KEY_COLUMNS)
    for prefix in (QUESTION_PREFIX, VOTE_COUNT_PREFIX, DISCORD_PREFIX):
        header += [f"{prefix}{finding}" for finding in questions]
    with table.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)

    long_form = None
    if variants is not None:
        long_form = directory / "exam_findings_variants.csv"
        fields = [
            "anon_mrn",
            "anon_accession_number",
            "finding_id",
            "variant_index",
            "label",
            "certainty",
            "severity",
        ]
        with long_form.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(variants)

    sidecar.write_text(
        json.dumps(
            {
                "written_at": datetime.now().isoformat(timespec="seconds"),
                "writer": "air_labels",
                "label_set_version": version,
                "vote_rule": "max over the wordings asked; UNANSWERED ignored unless every "
                             "wording returned it",
                "decode": decode or {},
                "n_exams": len(rows),
                "n_findings": len(questions),
                "n_variant_rows": len(variants) if variants is not None else 0,
                "column_prefixes": {
                    QUESTION_PREFIX: "the vote, 1, 0 or -1",
                    VOTE_COUNT_PREFIX: "how many wordings answered Yes",
                    DISCORD_PREFIX: "1 where a Yes rests on fewer wordings than asked",
                },
                "findings": questions,
            },
            indent=2,
        )
    )
    return table, sidecar, long_form


def main(
    answers: str,
    crosswalk: str,
    modality_config: str = "../rate/config/modalities/fast_abdomen_ct.yaml",
    output: str = "../ifast/data/fast_ct_25/metadata/labels",
    arrays: str | None = None,
    splits: str | None = None,
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
    splits : str, optional
        The split match list `air_split` wrote, used to fill `split` and
        `train_order`.
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
    wordings = sum(entry["n_wordings"] for entry in questions.values())
    logger.info(
        "The question set holds %d finding(s) over %d wording(s), version %s.",
        len(questions),
        wordings,
        version,
    )

    pairs = read_pairs(crosswalk)
    wanted = {pair["ct_accession_number"] for pair in pairs if pair["ct_accession_number"]}
    found, covariates, levels = read_answers(answers, wanted, set(questions))
    decode_version, decode = decode_digest(answers)
    if decode_version:
        logger.info("Decode configuration %s.", decode_version)

    split_map = None if splits is None else read_splits(splits)
    rows, variants, unreported = build_rows(
        pairs,
        found,
        questions,
        version,
        arrays,
        split_map,
        covariates=covariates,
        decode_version=decode_version,
        levels=levels,
    )
    positives = sum(
        1 for row in rows if any(row[key] == 1 for key in row if key.startswith(QUESTION_PREFIX))
    )
    logger.info(
        "%d exam pair(s), %d with at least one Yes, %d with no report. "
        "%d long-form wording row(s).",
        len(rows),
        positives,
        unreported,
        len(variants),
    )

    if dry_run:
        logger.info("Dry run: nothing written.")
        return

    table, sidecar, long_form = write_labels(
        rows, questions, output, version, variants=variants, decode=decode
    )
    logger.info("Wrote %s, %s and %s.", table, sidecar, long_form)


def cli() -> None:
    """Entry point for `air_labels`."""
    fire.Fire(main)


if __name__ == "__main__":
    cli()
