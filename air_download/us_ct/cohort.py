"""
cohort.py

Description: Download a matched ultrasound-CT cohort into one folder per
             patient and per visit, reading the paired CSV that `air_match`
             writes.

Each row of the matched CSV becomes ``<output>/<mrn>/<MM-DD-YY>/``, holding
the ultrasound under ``us/`` and the CT under ``ct/``. The visit folder is
named for the date of the ultrasound, since that is the exam the CT followed.
The CT is reduced to its structured report plus the thinnest axial series,
while every series of the ultrasound is kept.

Examples
--------
Preview the folders a run would create, without touching the network::

    pixi run download-cohort --matched_csv matched_us_ct.csv \
        --output output-cohort/ --dry_run

Download a single pair to verify the layout before scaling up::

    pixi run download-cohort --matched_csv matched_us_ct.csv \
        --output output-cohort/ --cred_path ~/air_login.txt --n 1

Download the whole cohort; exams already present are skipped, so the run
above is not repeated::

    pixi run download-cohort --matched_csv matched_us_ct.csv \
        --output output-cohort/ --cred_path ~/air_login.txt
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
from air_download.filters import DEFAULT_AXIAL_PATTERNS
from air_download.utils import configure_logging, parse_datetime

logger = logging.getLogger(__name__)

# Only what identifies the two exams of a visit. Everything else `air_match`
# writes is ignored, so a CSV from an older run still works.
REQUIRED_COLUMNS = (
    "mrn",
    "us_accession_number",
    "us_date_time",
    "ct_accession_number",
)

# One visit per patient per day, so the ultrasound's date names the folder.
VISIT_DATE_FORMAT = "%m-%d-%y"

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
        columns only.

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


def visit_folder_name(us_date_time: str, us_accession: str, ct_accession: str) -> str:
    """Name a visit folder after the date of its ultrasound.

    Parameters
    ----------
    us_date_time : str
        The ultrasound's timestamp, ISO 8601.
    us_accession : str
        The ultrasound's accession number, used only as a fallback.
    ct_accession : str
        The CT's accession number, used only as a fallback.

    Returns
    -------
    str
        The date as ``MM-DD-YY``, or the two accession numbers joined when
        the timestamp cannot be parsed.
    """
    try:
        return parse_datetime(us_date_time or "").strftime(VISIT_DATE_FORMAT)
    except ValueError:
        logger.warning(
            "A row's us_date_time could not be parsed; naming its visit "
            "folder after the accession pair instead."
        )
        return f"{us_accession}_{ct_accession}"


def build_visit_paths(
    output: Path,
    row: dict[str, str],
    claimed: dict[Path, tuple[str, str]] | None = None,
) -> tuple[Path, Path]:
    """Build the ultrasound and CT archive paths for one matched pair.

    Two visits for the same patient on the same day would name the same
    folder. That is not expected, so rather than merging them silently, a
    folder already claimed by a different pair gets an index suffix. Pass
    ``claimed`` to enable that bookkeeping across rows.

    Parameters
    ----------
    output : Path
        Root directory of the cohort.
    row : dict
        A row from :func:`read_matched_pairs`.
    claimed : dict, optional
        Maps an already-used visit directory to the accession pair holding
        it. Updated in place.

    Returns
    -------
    tuple of Path
        The ``us/`` and ``ct/`` archive paths, in that order.
    """
    mrn = _safe_component(row["mrn"])
    us_accession = _safe_component(row["us_accession_number"])
    ct_accession = _safe_component(row["ct_accession_number"])
    visit = _safe_component(
        visit_folder_name(row.get("us_date_time", ""), us_accession, ct_accession)
    )
    visit_dir = Path(output) / mrn / visit

    if claimed is not None:
        pair = (us_accession, ct_accession)
        candidate, index = visit_dir, 1
        while claimed.get(candidate, pair) != pair:
            index += 1
            candidate = visit_dir.with_name(f"{visit_dir.name}_{index}")
        if candidate != visit_dir:
            logger.warning(
                "A second visit falls on the same date for one patient; "
                "storing it in a suffixed folder rather than merging the two."
            )
        claimed[candidate] = pair
        visit_dir = candidate

    return (
        visit_dir / "us" / f"{us_accession}.zip",
        visit_dir / "ct" / f"{ct_accession}.zip",
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
) -> str:
    """Place one exam's archive at ``path``, returning what it took to do so."""
    if path.exists():
        if skip_existing and path.stat().st_size > 0:
            return "skipped"
        # build_exam_output_path indexes around an existing file, which would
        # write next to the target rather than over it.
        path.unlink()

    path.parent.mkdir(parents=True, exist_ok=True)

    previous = downloaded.get((mrn, accession))
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

    downloaded[(mrn, accession)] = path
    return "downloaded"


def download_cohort(
    matched_csv: str | Path,
    output: str | Path,
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

    The ultrasound keeps all of its series; the CT is reduced to its
    structured report plus the thinnest axial series, as ``--thinnest-axial``
    does. A failed exam is counted and the run continues, so one bad row
    cannot end a long download.

    Parameters
    ----------
    matched_csv : str or Path
        CSV of matched pairs, as written by ``air_match``.
    output : str or Path
        Root directory to write ``<mrn>/<MM-DD-YY>/{us,ct}/`` under.
    n : int, optional
        Download only the first ``n`` rows. Use it to verify one visit
        before committing to the whole cohort.
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
    rows = read_matched_pairs(Path(matched_csv))

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

    claimed: dict[Path, tuple[str, str]] = {}
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
        us_path, ct_path = build_visit_paths(output, row, claimed)
        exams = (
            (row["us_accession_number"], us_path, False),
            (row["ct_accession_number"], ct_path, True),
        )

        if dry_run:
            for _, path, _thinnest in exams:
                print(path)
            continue

        for accession, path, thinnest_axial in exams:
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
        logger.info("Dry run: %d visit(s) would be written under %s.", len(rows), output)
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
