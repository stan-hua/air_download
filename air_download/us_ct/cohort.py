"""
cohort.py

Description: Download a matched ultrasound-CT cohort into one folder per
             patient and per visit, reading the paired CSV that `air_match`
             writes.

Each row of the matched CSV becomes ``<output>/P0001/visit-01/``, holding the
ultrasound under ``us/`` and the CT under ``ct/``. Nothing in that path is an
identifier: patients, exams, and visits are all numbered by
:mod:`air_download.crosswalk`, which writes the only mapping back to real MRNs
and accession numbers -- to a file beside the cohort, never inside it. Visits
count within a patient in the order their ultrasounds happened, so the folder
still orders a patient's FAST-CT pairs without naming a date.

The CT is reduced to its thinnest axial series alone, while every series of the
ultrasound is kept.

Examples
--------
Preview the folders a run would create, without touching the network::

    pixi run download-cohort --matched_csv matched_us_ct.csv \
        --output output-cohort/ --dry_run

Download a single pair to verify the layout before scaling up::

    pixi run download-cohort --matched_csv matched_us_ct.csv \
        --output output-cohort/ --cred_path ~/air_login.txt --n 1

Download the whole cohort; exams already present are skipped and identifiers
already assigned are reused, so the run above is not repeated and nobody is
filed twice::

    pixi run download-cohort --matched_csv matched_us_ct.csv \
        --output output-cohort/ --cred_path ~/air_login.txt

Keep the crosswalk somewhere other than beside the cohort::

    pixi run download-cohort --matched_csv matched_us_ct.csv \
        --output output-cohort/ --crosswalk_csv ~/private/cohort_crosswalk.csv \
        --cred_path ~/air_login.txt
"""

# Standard libraries
import csv
import logging
import re
import shutil
from pathlib import Path

# Non-standard libraries
import fire
from tqdm import tqdm

# Custom libraries
from air_download.client import DEFAULT_MAX_RETRIES, AIRClient
from air_download.crosswalk import (
    Crosswalk,
    default_crosswalk_path,
    is_anon_mrn,
    parse_anon_ids,
)
from air_download.filters import DEFAULT_AXIAL_PATTERNS
from air_download.utils import configure_logging, converted_exam_path, parse_datetime

logger = logging.getLogger(__name__)

# Only what identifies the two exams of a visit. Everything else `air_match`
# writes is ignored, so a CSV from an older run still works.
REQUIRED_COLUMNS = (
    "mrn",
    "us_accession_number",
    "us_date_time",
    "ct_accession_number",
)

# Read when present, but not required: a matched CSV written before this
# column existed must still work, as with `us_image_count`.
OPTIONAL_COLUMNS = ("ct_date_time",)

_UNSAFE_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")


def read_matched_pairs(csv_path: Path) -> list[dict[str, str]]:
    """Read ultrasound-CT pairs from the CSV written by ``air_match``.

    Rows missing any required value are skipped and counted in a warning,
    and exact duplicate pairs are collapsed. Identifiers are never logged.

    Parameters
    ----------
    csv_path : Path
        CSV with ``mrn``, ``us_accession_number``, ``us_date_time``, and
        ``ct_accession_number`` columns. Extra columns are ignored.

    Returns
    -------
    list of dict
        One entry per usable row, in file order, holding the required
        columns plus any of :data:`OPTIONAL_COLUMNS` the CSV carried.

    Raises
    ------
    ValueError
        If the CSV lacks any required column.
    """
    csv_path = Path(csv_path)
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise ValueError(
                f"{csv_path} is missing required column(s): "
                f"{', '.join(missing)}. Found: "
                f"{', '.join(fieldnames) if fieldnames else '(no header row)'}."
            )

        rows: list[dict[str, str]] = []
        seen: set[tuple[str, ...]] = set()
        incomplete = 0
        for raw in reader:
            row = {c: (raw.get(c) or "").strip() for c in REQUIRED_COLUMNS}
            if not all(row.values()):
                incomplete += 1
                continue
            row.update({c: (raw.get(c) or "").strip() for c in OPTIONAL_COLUMNS})
            key = tuple(row[c] for c in REQUIRED_COLUMNS)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

    if incomplete:
        logger.warning(
            "Skipped %d row(s) in %s missing an MRN, an accession number, or "
            "an ultrasound date. All four are needed to place a visit.",
            incomplete,
            csv_path,
        )
    logger.info("Read %d unique matched pair(s) from %s.", len(rows), csv_path)
    return rows


def _safe_component(value: str) -> str:
    """Reduce a value to a single filesystem-safe path segment."""
    cleaned = _UNSAFE_CHARACTERS.sub("_", value.strip())
    return cleaned if cleaned.strip(".") else "_"


def sort_rows_for_numbering(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Order rows so each patient's visits are numbered chronologically.

    Patients keep the order they first appear in, so identifiers stay easy to
    trace back to the source CSV; within a patient, rows are ordered by the
    ultrasound's timestamp. Rows whose timestamp will not parse sort last and
    keep their relative order, since there is nothing better to go on.

    Parameters
    ----------
    rows : list of dict
        Rows from :func:`read_matched_pairs`.

    Returns
    -------
    list of dict
        The same rows, reordered.
    """
    first_seen: dict[str, int] = {}
    for row in rows:
        first_seen.setdefault(row["mrn"], len(first_seen))

    def key(row: dict[str, str]) -> tuple[int, int, float]:
        try:
            when = parse_datetime(row.get("us_date_time") or "")
        except ValueError:
            return (first_seen[row["mrn"]], 1, 0.0)
        return (first_seen[row["mrn"]], 0, when.timestamp())

    return sorted(rows, key=key)


def build_visit_paths(
    output: Path, row: dict[str, str], crosswalk: Crosswalk
) -> tuple[Path, Path]:
    """Build the ultrasound and CT archive paths for one matched pair.

    Every component comes from ``crosswalk``, so no part of the returned path
    is an identifier. The visit ordinal is unique within its patient by
    construction, which is why two pairs on the same date can no longer
    collide.

    Parameters
    ----------
    output : Path
        Root directory of the cohort.
    row : dict
        A row from :func:`read_matched_pairs`.
    crosswalk : Crosswalk
        Assigns and remembers the anonymous identifiers. Required: falling
        back to real ones is the leak this layout exists to close.

    Returns
    -------
    tuple of Path
        The ``us/`` and ``ct/`` archive paths, in that order.
    """
    mrn = row["mrn"]
    us_accession = row["us_accession_number"]
    ct_accession = row["ct_accession_number"]

    anon_mrn = crosswalk.patient_id(mrn)
    anon_us = crosswalk.exam_id(mrn, us_accession)
    anon_ct = crosswalk.exam_id(mrn, ct_accession)
    visit = crosswalk.visit_folder(
        mrn, us_accession, ct_accession, row.get("us_date_time", "")
    )

    # Every component is generated, so _safe_component has nothing left to
    # sanitise. Kept anyway: it is what makes a hostile CSV structurally
    # unable to escape the output directory.
    visit_dir = Path(output) / _safe_component(anon_mrn) / _safe_component(visit)
    return (
        visit_dir / "us" / f"{_safe_component(anon_us)}.zip",
        visit_dir / "ct" / f"{_safe_component(anon_ct)}.zip",
    )


def _fetch_exam(
    client: AIRClient,
    mrn: str,
    accession: str,
    path: Path,
    thinnest_axial: bool,
    axial_patterns: str,
    project: int | None,
    profile: int | None,
    skip_existing: bool,
    downloaded: dict[tuple[str, str], Path],
    dedupe_key: tuple[str, str],
    converted: Path | None = None,
) -> str:
    """Place one exam's archive at ``path``, returning what it took to do so."""
    # An archive deleted after conversion must not be downloaded again. The
    # converted array is the evidence the exam is done, and it is the only
    # evidence left once `air_convert --delete_source` has run.
    if skip_existing and converted is not None and converted.exists():
        return "skipped"

    if path.exists():
        if skip_existing and path.stat().st_size > 0:
            return "skipped"
        # build_exam_output_path indexes around an existing file, which would
        # write next to the target rather than over it.
        path.unlink()

    path.parent.mkdir(parents=True, exist_ok=True)

    # Keyed on the anonymous pair: nothing outliving a single call should
    # hold a real identifier, not even in memory.
    previous = downloaded.get(dedupe_key)
    if previous is not None and previous.exists():
        logger.warning(
            "An exam downloaded earlier in this run belongs to a second "
            "visit as well; copying it instead of downloading it again."
        )
        shutil.copy2(previous, path)
        return "copied"

    client.download(
        accession=accession,
        mrn=mrn,
        output=path,
        project=project,
        profile=profile,
        thinnest_axial=thinnest_axial,
        axial_patterns=axial_patterns,
    )
    if not path.exists():
        # A search that matched nothing returns without writing; counting it
        # as downloaded would report a complete cohort that has a hole in it.
        logger.warning(
            "No archive was written for one exam; the search matched "
            "nothing for that MRN and accession pair."
        )
        return "failed"

    downloaded[dedupe_key] = path
    return "downloaded"


def _warn_about_a_pre_pseudonym_tree(output: Path) -> None:
    """Warn when an output directory predates pseudonymous folder names."""
    if not output.is_dir():
        return
    stale = [p for p in output.iterdir() if p.is_dir() and not is_anon_mrn(p.name)]
    if stale:
        logger.warning(
            "%d folder(s) under %s are not named for an anonymous patient ID. "
            "They were written by an older version, will not be resumed, and "
            "their names are identifiers. Move or delete them.",
            len(stale),
            output,
        )


def download_cohort(
    matched_csv: str | Path,
    output: str | Path,
    crosswalk_csv: str | Path | None = None,
    converted_dir: str | Path | None = None,
    n: int | None = None,
    url: str | None = None,
    cred_path: str | Path | None = None,
    project: int | None = None,
    profile: int | None = None,
    axial_patterns: str = DEFAULT_AXIAL_PATTERNS,
    skip_existing: bool = True,
    dry_run: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
    verbose: bool = False,
    quiet: bool = False,
) -> None:
    """Download every matched ultrasound-CT pair into per-visit folders.

    Exams land under ``<output>/P0001/visit-01/{us,ct}/A0001.zip``. No part of
    that is an identifier; the crosswalk beside the cohort holds the only way
    back, and re-running reuses what it already assigned, so an interrupted
    download resumes rather than filing anyone twice.

    The ultrasound keeps all of its series; the CT is reduced to its
    thinnest axial series alone, as ``--thinnest-axial`` does. A CT with no
    axial series yields no archive and is counted as failed. A failed exam
    is counted and the run continues, so one bad row cannot end a long
    download.

    Parameters
    ----------
    matched_csv : str or Path
        CSV of matched pairs, as written by ``air_match``.
    output : str or Path
        Root directory to write ``P0001/visit-01/{us,ct}/`` under.
    crosswalk_csv : str or Path, optional
        Where the pseudonym mapping lives. Defaults to ``<output>_crosswalk.csv``
        beside the cohort. A path inside ``output`` is refused, so archiving
        the cohort cannot ship the key with the lock.
    converted_dir : str or Path, optional
        Root of the tree ``air_convert`` writes. When given, an exam whose
        converted array is already there is skipped without downloading.
        This is what makes a batched ingest resumable: once
        ``air_convert --delete_source`` has removed an archive, its converted
        array is the only remaining evidence the exam was ever fetched, and
        without this the next run would download the whole cohort again.
    n : int, optional
        Download only the first ``n`` visits, ordered as they will be
        numbered. Use it to verify one visit before committing to the whole
        cohort.
    url : str, optional
        AIR API URL. Falls back to the credential file or environment.
    cred_path : str or Path, optional
        Login credentials file, dotenv format.
    project : int, optional
        Project ID. Falls back to ``AIR_PROJECT``.
    profile : int, optional
        Anonymization profile ID. Falls back to ``AIR_PROFILE``.
    axial_patterns : str
        Comma-separated plane names identifying an axial CT series.
    skip_existing : bool
        Skip an exam whose archive is already present and non-empty, which
        is what makes an interrupted run cheap to resume.
    dry_run : bool
        Print the paths that would be written and make no network call.
    max_retries : int
        Retry attempts per request.
    verbose : bool
        Log at DEBUG.
    quiet : bool
        Log at ERROR only.
    """
    configure_logging(verbose, quiet)

    output = Path(output)
    crosswalk_path = (
        Path(crosswalk_csv)
        if crosswalk_csv is not None
        else default_crosswalk_path(output)
    )
    if crosswalk_path.resolve().is_relative_to(output.resolve()):
        raise ValueError(
            f"The crosswalk ({crosswalk_path}) is inside the cohort "
            f"({output}). It is the only way back to real identifiers, so "
            f"keeping it there means any copy of the cohort carries the key "
            f"with the lock. Put it somewhere else."
        )

    rows = read_matched_pairs(Path(matched_csv))
    # Sort before slicing, so `--n 1` picks the same visit the full run would
    # number first and the two runs assign identical identifiers.
    rows = sort_rows_for_numbering(rows)

    if n is not None:
        if n < 1:
            raise ValueError(f"n must be at least 1, got {n}.")
        if n < len(rows):
            logger.warning(
                "Downloading the first %d of %d matched pair(s); this run "
                "covers part of the cohort only.",
                n,
                len(rows),
            )
        rows = rows[:n]

    if not rows:
        logger.warning("No usable rows in %s; nothing to download.", matched_csv)
        return

    converted_root = None if converted_dir is None else Path(converted_dir)

    _warn_about_a_pre_pseudonym_tree(output)
    crosswalk = Crosswalk(crosswalk_path, read_only=dry_run)
    downloaded: dict[tuple[str, str], Path] = {}
    counts = {"downloaded": 0, "skipped": 0, "copied": 0, "failed": 0}

    client = (
        None
        if dry_run
        else AIRClient(url=url, cred_path=cred_path, max_retries=max_retries)
    )

    for row in tqdm(
        rows, desc="Downloading visits", total=len(rows), disable=dry_run
    ):
        us_path, ct_path = build_visit_paths(output, row, crosswalk)
        exams = (
            ("us", row["us_accession_number"], row.get("us_date_time", ""), us_path, False),
            ("ct", row["ct_accession_number"], row.get("ct_date_time", ""), ct_path, True),
        )

        # Recorded before the download, not after: a crosswalk row for an
        # exam that then failed costs nothing, while an archive on disk with
        # no way back cannot be repaired.
        for exam_type, accession, when, path, _thinnest in exams:
            crosswalk.record(
                mrn=row["mrn"],
                accession=accession,
                exam_type=exam_type,
                date_time=when,
                visit_folder=path.parent.parent.name,
                archive_path=path.relative_to(output),
            )

        if dry_run:
            for _type, _accession, _when, path, _thinnest in exams:
                print(path)
            continue

        for _exam_type, accession, _when, path, thinnest_axial in exams:
            try:
                outcome = _fetch_exam(
                    client=client,
                    mrn=row["mrn"],
                    accession=accession,
                    path=path,
                    thinnest_axial=thinnest_axial,
                    axial_patterns=axial_patterns,
                    project=project,
                    profile=profile,
                    skip_existing=skip_existing,
                    downloaded=downloaded,
                    dedupe_key=parse_anon_ids(path.relative_to(output)),
                    converted=(
                        None
                        if converted_root is None
                        else converted_exam_path(path, output, converted_root)
                    ),
                )
                counts[outcome] += 1
            except Exception as exc:  # noqa: BLE001 - one bad exam is not fatal
                counts["failed"] += 1
                logger.error(
                    "An exam failed to download (%s); continuing with the "
                    "rest. Re-run to retry it.",
                    exc.__class__.__name__,
                )
                # Only at DEBUG: the detail can carry an identifier.
                logger.debug("Failure detail: %s", exc)

    if dry_run:
        logger.info(
            "Dry run: %d visit(s) would be written under %s, and no crosswalk "
            "was written.",
            len(rows),
            output,
        )
        return

    logger.info(
        "Cohort complete across %d visit(s): %d exam(s) downloaded, %d "
        "already present, %d copied from an earlier visit, %d failed.",
        len(rows),
        counts["downloaded"],
        counts["skipped"],
        counts["copied"],
        counts["failed"],
    )
    logger.info(
        "%d patient(s) and %d archive(s) are mapped in %s. That file is the "
        "only way back to real MRNs and accession numbers: keep it out of "
        "the cohort directory, out of git, and out of anything you share.",
        crosswalk.n_patients,
        len(crosswalk),
        crosswalk_path,
    )
    if counts["failed"]:
        logger.warning(
            "%d exam(s) failed. Re-running skips what is already downloaded.",
            counts["failed"],
        )


def cli() -> None:
    """CLI entry point."""
    fire.Fire(download_cohort)


if __name__ == "__main__":
    cli()
