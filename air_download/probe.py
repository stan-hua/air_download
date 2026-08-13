"""
probe.py

Description: List the series inside candidate exams without downloading them,
             so you can tell which exam is the one you want before committing
             to a retrieval.

The AIR API reports only a description, a modality, a series number, a series
UID, and an ``imageCount`` per series. ``imageCount`` counts DICOM *objects*,
not frames: a multi-frame ultrasound cine clip counts once however long it
runs, and neither slice thickness nor imaging plane is exposed at all. So a
count here is a proxy for how many views an exam holds, not for how much pixel
data it carries. Treat it as the heuristic it is.

Each exam costs two plain queries and no image transfer, since
``/secure/search/series`` sits outside the download handshake. No project or
anonymization profile is needed, because nothing is retrieved.

A matched CSV names one ``<modality>_accession_number`` column per modality
it pairs, so the modalities are read off its header rather than assumed. Any
pairing works, not only ultrasound and CT.

Examples
--------
Summarise every exam of a matched cohort, one row per exam::

    pixi run probe --input_csv matched_us_ct.csv --summary \
        --output probe.csv --cred_path ~/air_login.txt

List every series of every exam in a search result::

    pixi run probe --input_csv output-us_ed_bedside/accessions.csv \
        --output probe_series.csv --cred_path ~/air_login.txt

Probe one side of the pairing only, a couple of exams at a time::

    pixi run probe --input_csv matched_us_ct.csv --modalities ct --n 2 \
        --cred_path ~/air_login.txt

Pick two modalities out of a wider pairing::

    pixi run probe --input_csv matched_us_ct_mr.csv --modalities us,mr \
        --cred_path ~/air_login.txt

Check which CT series ``--thinnest-axial`` would actually keep, before
downloading anything::

    pixi run probe --input_csv matched_us_ct.csv --modalities ct \
        --select thinnest_axial --cred_path ~/air_login.txt
"""

# Standard libraries
import csv
import logging
import re
from pathlib import Path
from typing import Any

# Non-standard libraries
import fire
from tqdm import tqdm

# Custom libraries
from air_download.client import DEFAULT_MAX_RETRIES, AIRClient
from air_download.filters import (
    DEFAULT_AXIAL_PATTERNS,
    parse_axial_patterns,
    select_thinnest_axial,
)
from air_download.utils import configure_logging, read_accession_pairs

logger = logging.getLogger(__name__)

# A matched CSV names one accession column per modality it pairs, so the
# modalities are read off the header rather than hard-coded. This is what
# keeps the command working for pairings beyond ultrasound and CT.
_ACCESSION_COLUMN = re.compile(r"^(?P<modality>.+)_accession_number$")

# Probe every modality the CSV declares. Ignored for a search-result CSV,
# which holds one unprefixed accession per row.
ALL_MODALITIES = "all"
DEFAULT_MODALITIES = ALL_MODALITIES

PER_SERIES_HEADER = [
    "mrn",
    "accession_number",
    "date_time",
    "description",
    "study_image_count",
    "series_number",
    "series_description",
    "series_modality",
    "series_image_count",
    "series_uid",
]

SUMMARY_HEADER = [
    "mrn",
    "accession_number",
    "date_time",
    "description",
    "study_image_count",
    "n_series",
    "total_series_image_count",
    "series_descriptions",
]

# Appended when --select is in play. Rows are marked rather than dropped, so
# the output still shows what the selection passed over.
PER_SERIES_SELECT_COLUMNS = ["selected"]
SUMMARY_SELECT_COLUMNS = ["selected_description", "selected_image_count"]

# The only selection worth previewing: what `--thinnest-axial` would keep.
THINNEST_AXIAL = "thinnest_axial"
SELECT_CHOICES = (THINNEST_AXIAL,)

# Separator for the joined series descriptions of a summary row.
_DESCRIPTION_SEPARATOR = " | "


def matched_modalities(fieldnames: list[str]) -> list[str]:
    """Return the modalities a matched CSV's header declares.

    A matched CSV names its accession columns ``<modality>_accession_number``,
    so the pairing's modalities can be read off the header. An unprefixed
    ``accession_number`` does not match, which is what tells a search-result
    CSV apart from a matched one.

    Parameters
    ----------
    fieldnames : list of str
        The CSV's header row.

    Returns
    -------
    list of str
        The modalities found, lowercased, in header order and without
        duplicates. Empty for a CSV that pairs nothing.
    """
    found: list[str] = []
    for name in fieldnames:
        match = _ACCESSION_COLUMN.match((name or "").strip())
        if match is None:
            continue
        modality = match.group("modality").lower()
        if modality not in found:
            found.append(modality)
    return found


def _resolve_modalities(requested: Any, available: list[str]) -> list[str]:
    """Narrow the modalities a CSV declares to the ones asked for."""
    if isinstance(requested, (list, tuple)):
        names = [str(m).strip().lower() for m in requested]
    else:
        names = [m.strip().lower() for m in str(requested).split(",")]
    names = [m for m in names if m]

    if not names:
        raise ValueError(
            f"No modality requested. Pass '{ALL_MODALITIES}' or any of: "
            f"{', '.join(available)}."
        )
    if names == [ALL_MODALITIES]:
        return list(available)

    unknown = [m for m in names if m not in available]
    if unknown:
        raise ValueError(
            f"The CSV has no column(s) for {', '.join(unknown)}. It pairs: "
            f"{', '.join(available)}. Expected a "
            f"'<modality>_accession_number' column per modality."
        )
    # Preserve the caller's order, minus repeats.
    return list(dict.fromkeys(names))


def read_exam_pairs(
    input_csv: str | Path, modalities: Any = DEFAULT_MODALITIES
) -> list[tuple[str, str]]:
    """Read the (MRN, accession number) pairs to probe.

    Accepts either CSV this package writes: a matched CSV pairing any set of
    modalities, or the search results from ``--search-only``. The two are told
    apart by their header. Both identifiers are always carried together, since
    an accession number alone can belong to more than one patient.

    Parameters
    ----------
    input_csv : str or Path
        A matched CSV (one ``<modality>_accession_number`` column per paired
        modality) or a search-result CSV (an unprefixed ``accession_number``).
    modalities : str or list, optional
        Which of a matched CSV's modalities to probe: ``all`` (the default),
        or a comma-separated selection such as ``us,ct``. Ignored for a
        search-result CSV.

    Returns
    -------
    list of tuple
        Unique (MRN, accession number) pairs, in file order.

    Raises
    ------
    ValueError
        If a requested modality is not in the CSV, or the CSV lacks ``mrn``.
    """
    input_csv = Path(input_csv)
    with open(input_csv, newline="") as f:
        # DictReader consumes only the header line.
        fieldnames = csv.DictReader(f).fieldnames or []

    available = matched_modalities(fieldnames)
    if not available:
        if str(modalities).strip().lower() != ALL_MODALITIES:
            logger.warning(
                "%s pairs no modalities, so it holds one accession per row "
                "and every row is probed; the requested modalities are "
                "ignored.",
                input_csv,
            )
        return read_accession_pairs(input_csv)

    wanted = _resolve_modalities(modalities, available)
    if "mrn" not in fieldnames:
        raise ValueError(
            f"{input_csv} is missing required column: mrn. Found: "
            f"{', '.join(fieldnames) if fieldnames else '(no header row)'}."
        )

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    incomplete = 0
    with open(input_csv, newline="") as f:
        for row in csv.DictReader(f):
            mrn = (row.get("mrn") or "").strip()
            for modality in wanted:
                accession = (
                    row.get(f"{modality}_accession_number") or ""
                ).strip()
                if not mrn or not accession:
                    incomplete += 1
                    continue
                pair = (mrn, accession)
                if pair in seen:
                    continue
                seen.add(pair)
                pairs.append(pair)

    if incomplete:
        logger.warning(
            "Skipped %d entr(ies) in %s missing an MRN or accession number. "
            "Both are required: an accession number alone can match more "
            "than one patient.",
            incomplete,
            input_csv,
        )
    logger.info(
        "Read %d unique exam(s) to probe from %s (%s of: %s).",
        len(pairs),
        input_csv,
        ", ".join(wanted),
        ", ".join(available),
    )
    return pairs


def _as_count(value: Any) -> int:
    """Return a count as an int, treating anything unusable as zero."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _series_rows(
    mrn: str,
    exam: dict[str, Any],
    series: list[dict[str, Any]],
    chosen: dict[str, Any] | None = None,
    mark_selection: bool = False,
) -> list[dict[str, Any]]:
    """Build one output row per series of an exam."""
    common = {
        "mrn": mrn,
        "accession_number": exam.get("accessionNumber", ""),
        "date_time": exam.get("dateTime", ""),
        "description": exam.get("description", ""),
        "study_image_count": exam.get("imageCount", ""),
    }
    rows = []
    for s in series:
        row = {
            **common,
            "series_number": s.get("seriesNumber", ""),
            "series_description": s.get("description", ""),
            "series_modality": s.get("modality", ""),
            "series_image_count": s.get("imageCount", ""),
            "series_uid": s.get("seriesUid", ""),
        }
        if mark_selection:
            # Identity, not equality: two series can carry the same fields.
            row["selected"] = chosen is not None and s is chosen
        rows.append(row)
    return rows


def _summary_row(
    mrn: str,
    exam: dict[str, Any],
    series: list[dict[str, Any]],
    chosen: dict[str, Any] | None = None,
    mark_selection: bool = False,
) -> dict[str, Any]:
    """Reduce an exam's series to the single row you sort candidates on."""
    row = {
        "mrn": mrn,
        "accession_number": exam.get("accessionNumber", ""),
        "date_time": exam.get("dateTime", ""),
        "description": exam.get("description", ""),
        "study_image_count": exam.get("imageCount", ""),
        "n_series": len(series),
        "total_series_image_count": sum(_as_count(s.get("imageCount")) for s in series),
        "series_descriptions": _DESCRIPTION_SEPARATOR.join(
            (s.get("description") or "").strip() for s in series
        ),
    }
    if mark_selection:
        # Blank rather than zero when nothing qualifies, so "no axial series
        # here" cannot be misread as "an axial series holding no images".
        row["selected_description"] = (
            (chosen.get("description") or "").strip() if chosen else ""
        )
        row["selected_image_count"] = (
            _as_count(chosen.get("imageCount")) if chosen else ""
        )
    return row


def probe_csv_header(summary: bool = False, mark_selection: bool = False) -> list[str]:
    """Return the column names for a given combination of output options."""
    header = list(SUMMARY_HEADER if summary else PER_SERIES_HEADER)
    if mark_selection:
        header += SUMMARY_SELECT_COLUMNS if summary else PER_SERIES_SELECT_COLUMNS
    return header


def write_probe_csv(
    rows: list[dict[str, Any]],
    output: Path,
    summary: bool = False,
    mark_selection: bool = False,
) -> Path:
    """Write probe results to a CSV, overwriting any existing file.

    Overwrites rather than appends, unlike the search results, so re-probing
    after a failed run does not double every row.

    Parameters
    ----------
    rows : list of dict
        Rows keyed as :func:`probe_csv_header` describes.
    output : Path
        Destination path.
    summary : bool, optional
        Whether ``rows`` are summary rows.
    mark_selection : bool, optional
        Whether the rows carry the selection columns.

    Returns
    -------
    Path
        The path written.
    """
    output = Path(output)
    if output.parent != Path(""):
        output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=probe_csv_header(summary, mark_selection)
        )
        writer.writeheader()
        writer.writerows(rows)
    return output


def probe_series(
    input_csv: str | Path,
    output: str | Path = "probe_series.csv",
    modalities: Any = DEFAULT_MODALITIES,
    summary: bool = False,
    select: str | None = None,
    axial_patterns: str = DEFAULT_AXIAL_PATTERNS,
    n: int | None = None,
    url: str | None = None,
    cred_path: str | Path | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    verbose: bool = False,
    quiet: bool = False,
) -> None:
    """List the series of every exam in a CSV, without downloading any of them.

    Two queries per exam and no image transfer, so this is cheap to run over a
    whole cohort. An exam that fails is counted and the run continues.

    Parameters
    ----------
    input_csv : str or Path
        Matched CSV from ``air_match``, or a search-result ``accessions.csv``.
    output : str or Path, optional
        Where to write the results.
    modalities : str or list, optional
        Which of a matched CSV's modalities to probe: ``all`` (the default),
        or a comma-separated selection such as ``us,ct``.
    summary : bool, optional
        Write one row per exam (series count, total objects, joined
        descriptions) rather than one row per series.
    select : str, optional
        Mark what a series selection would keep, without dropping the rest.
        Only ``thinnest_axial`` is supported, mirroring ``--thinnest-axial``
        on the downloader. Adds a ``selected`` column per series, or the
        chosen series' description and count to a summary row.
    axial_patterns : str, optional
        Comma-separated plane names identifying an axial series, used only
        when ``select`` is set.
    n : int, optional
        Probe only the first ``n`` exams.
    url : str, optional
        AIR API URL. Falls back to the credential file or environment.
    cred_path : str or Path, optional
        Login credentials file, dotenv format.
    max_retries : int, optional
        Retry attempts per request.
    verbose : bool, optional
        Log at DEBUG.
    quiet : bool, optional
        Log at ERROR only.

    Raises
    ------
    ValueError
        If ``n`` is less than 1, or a requested modality is not in the CSV.
    """
    configure_logging(verbose, quiet)

    if select is not None and select not in SELECT_CHOICES:
        raise ValueError(
            f"Unknown select '{select}'. Choose one of: "
            f"{', '.join(SELECT_CHOICES)}."
        )
    mark_selection = select is not None
    patterns = parse_axial_patterns(axial_patterns) if mark_selection else []

    pairs = read_exam_pairs(input_csv, modalities)

    if n is not None:
        if n < 1:
            raise ValueError(f"n must be at least 1, got {n}.")
        if n < len(pairs):
            logger.warning(
                "Probing the first %d of %d exam(s); this run covers part of "
                "the input only.",
                n,
                len(pairs),
            )
        pairs = pairs[:n]

    if not pairs:
        logger.warning("No usable rows in %s; nothing to probe.", input_csv)
        return

    client = AIRClient(url=url, cred_path=cred_path, max_retries=max_retries)

    rows: list[dict[str, Any]] = []
    counts = {"probed": 0, "not_found": 0, "failed": 0, "nothing_selected": 0}

    for mrn, accession in tqdm(pairs, desc="Probing exams", total=len(pairs)):
        try:
            exams = client.search(accession=accession, mrn=mrn)
            if not exams:
                counts["not_found"] += 1
                continue
            if len(exams) > 1:
                logger.warning(
                    "One MRN and accession pair matched %d exams; probing the "
                    "first.",
                    len(exams),
                )
            exam = exams[0]
            series = client.list_series(exam)
            # The picker rather than keep_thinnest_axial: it makes the same
            # choice but stays quiet when nothing qualifies, which is the
            # normal case for every non-CT exam being previewed.
            chosen = select_thinnest_axial(series, patterns) if mark_selection else None
            if mark_selection and chosen is None:
                counts["nothing_selected"] += 1
            if summary:
                rows.append(
                    _summary_row(mrn, exam, series, chosen, mark_selection)
                )
            else:
                rows.extend(
                    _series_rows(mrn, exam, series, chosen, mark_selection)
                )
            counts["probed"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad exam is not fatal
            counts["failed"] += 1
            logger.error(
                "An exam failed to probe (%s); continuing with the rest.",
                exc.__class__.__name__,
            )
            # Only at DEBUG: the detail can carry an identifier.
            logger.debug("Failure detail: %s", exc)

    written = write_probe_csv(rows, Path(output), summary, mark_selection)
    logger.info(
        "Probed %d exam(s) into %d row(s), written to %s. %d exam(s) matched "
        "nothing, %d failed. Nothing was downloaded.",
        counts["probed"],
        len(rows),
        written,
        counts["not_found"],
        counts["failed"],
    )
    if counts["failed"]:
        logger.warning("%d exam(s) failed; re-run to retry them.", counts["failed"])
    if counts["nothing_selected"]:
        logger.warning(
            "%d of %d probed exam(s) have no axial series matching %s, so "
            "'--thinnest-axial' would download nothing for them. Expected "
            "for non-CT exams; for a CT it means the patterns need widening.",
            counts["nothing_selected"],
            counts["probed"],
            patterns,
        )


def cli() -> None:
    """CLI entry point."""
    fire.Fire(probe_series)


if __name__ == "__main__":
    cli()
