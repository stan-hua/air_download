"""
match.py

Description: Match ED ultrasound exams to the CT exams that followed them,
             pairing the two search-result CSVs produced by `--search-only`.

A pair qualifies when both exams belong to the same patient, the CT is
strictly after the ultrasound, and the two fall within a time window
(24 hours by default). Matching is on MRN, because an accession number
alone can belong to more than one patient.

Examples
--------
Match with the defaults, writing matched_us_ct.csv::

    pixi run match --us_csv output-us_ed_bedside/accessions.csv \
                   --ct_csv output-ct_abdomen_pelvis/accessions.csv

Widen the window and keep every qualifying CT per ultrasound::

    pixi run match --us_csv us/accessions.csv --ct_csv ct/accessions.csv \
                   --max_hours 48 --all_pairs --output matched_48h.csv
"""

# Standard libraries
import csv
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# Non-standard libraries
import fire

# Custom libraries
from air_download.utils import parse_datetime

logger = logging.getLogger(__name__)

DEFAULT_MAX_HOURS = 24.0

MATCH_CSV_HEADER = [
    "mrn",
    "us_accession_number",
    "us_date_time",
    "us_description",
    "ct_accession_number",
    "ct_date_time",
    "ct_description",
    "hours_between",
]

_REQUIRED_COLUMNS = ("mrn", "accession_number", "date_time")


def read_exams(csv_path: Path) -> list[dict[str, Any]]:
    """Read exams from a search-result CSV, parsing their timestamps.

    Rows missing an MRN or an unparseable date are dropped and counted in a
    warning; identifiers are never logged.

    Parameters
    ----------
    csv_path : Path
        CSV with ``mrn``, ``accession_number``, and ``date_time`` columns,
        as written by ``--search-only``.

    Returns
    -------
    list of dict
        One entry per usable row, with a ``when`` key holding the parsed
        timestamp alongside the original column values.
    """
    csv_path = Path(csv_path)
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [c for c in _REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise ValueError(
                f"{csv_path} is missing required column(s): "
                f"{', '.join(missing)}. Found: "
                f"{', '.join(fieldnames) if fieldnames else '(no header row)'}."
            )

        exams: list[dict[str, Any]] = []
        no_mrn = 0
        bad_date = 0
        for row in reader:
            mrn = (row.get("mrn") or "").strip()
            if not mrn:
                no_mrn += 1
                continue
            raw_date = (row.get("date_time") or "").strip()
            try:
                when = parse_datetime(raw_date)
            except ValueError:
                bad_date += 1
                continue
            exams.append({**row, "mrn": mrn, "when": when})

    if no_mrn:
        logger.warning("Skipped %d row(s) without an MRN in %s.", no_mrn, csv_path)
    if bad_date:
        logger.warning(
            "Skipped %d row(s) with an unreadable date_time in %s.",
            bad_date,
            csv_path,
        )
    logger.info("Read %d usable exam(s) from %s.", len(exams), csv_path)
    return exams


def _by_patient(exams: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group exams by MRN, each group sorted by time."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for exam in exams:
        grouped[exam["mrn"]].append(exam)
    for group in grouped.values():
        group.sort(key=lambda e: e["when"])
    return grouped


def _hours_between(earlier: datetime, later: datetime) -> float:
    """Return the gap between two timestamps in hours."""
    return (later - earlier).total_seconds() / 3600.0


def match_exams(
    us_exams: list[dict[str, Any]],
    ct_exams: list[dict[str, Any]],
    max_hours: float = DEFAULT_MAX_HOURS,
    all_pairs: bool = False,
) -> list[dict[str, Any]]:
    """Pair each ultrasound with the CT exam that followed it.

    A CT qualifies when it belongs to the same patient, falls strictly after
    the ultrasound, and is no more than ``max_hours`` later. Exams sharing a
    timestamp do not qualify, since neither can be said to have followed the
    other.

    Parameters
    ----------
    us_exams : list of dict
        Ultrasound exams, as returned by :func:`read_exams`.
    ct_exams : list of dict
        CT exams, as returned by :func:`read_exams`.
    max_hours : float, optional
        Widest gap to accept, in hours.
    all_pairs : bool, optional
        If True, emit every qualifying CT per ultrasound. If False (the
        default), emit only the earliest one.

    Returns
    -------
    list of dict
        Matched rows ordered by MRN then ultrasound time, keyed by
        :data:`MATCH_CSV_HEADER`.
    """
    if max_hours <= 0:
        raise ValueError(f"max_hours must be positive, got {max_hours}.")

    ct_by_patient = _by_patient(ct_exams)
    matches: list[dict[str, Any]] = []

    for mrn, patient_us in sorted(_by_patient(us_exams).items()):
        patient_ct = ct_by_patient.get(mrn)
        if not patient_ct:
            continue
        for us in patient_us:
            # patient_ct is time-sorted, so qualifying CTs come out earliest
            # first and the default branch can stop at the first one.
            qualifying = [
                ct
                for ct in patient_ct
                if 0 < _hours_between(us["when"], ct["when"]) <= max_hours
            ]
            for ct in qualifying if all_pairs else qualifying[:1]:
                matches.append(
                    {
                        "mrn": mrn,
                        "us_accession_number": us.get("accession_number", ""),
                        "us_date_time": us.get("date_time", ""),
                        "us_description": us.get("description", ""),
                        "ct_accession_number": ct.get("accession_number", ""),
                        "ct_date_time": ct.get("date_time", ""),
                        "ct_description": ct.get("description", ""),
                        "hours_between": round(
                            _hours_between(us["when"], ct["when"]), 3
                        ),
                    }
                )
    return matches


def write_matches_csv(matches: list[dict[str, Any]], output: Path) -> Path:
    """Write matched pairs to a CSV, overwriting any existing file.

    Parameters
    ----------
    matches : list of dict
        Rows from :func:`match_exams`.
    output : Path
        Destination path.

    Returns
    -------
    Path
        The path written.
    """
    output = Path(output)
    if output.parent != Path(""):
        output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MATCH_CSV_HEADER)
        writer.writeheader()
        writer.writerows(matches)
    return output


def match(
    us_csv: str,
    ct_csv: str,
    output: str = "matched_us_ct.csv",
    max_hours: float = DEFAULT_MAX_HOURS,
    all_pairs: bool = False,
    verbose: bool = False,
) -> None:
    """Match ultrasound exams to the CTs that followed, and write a CSV.

    Parameters
    ----------
    us_csv : str
        Search-result CSV of ultrasound exams.
    ct_csv : str
        Search-result CSV of CT exams.
    output : str, optional
        Where to write the matched pairs.
    max_hours : float, optional
        Widest gap between the ultrasound and the CT, in hours.
    all_pairs : bool, optional
        Emit every qualifying CT per ultrasound rather than the earliest.
    verbose : bool, optional
        Log at DEBUG level.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    us_exams = read_exams(Path(us_csv))
    ct_exams = read_exams(Path(ct_csv))
    matches = match_exams(us_exams, ct_exams, max_hours, all_pairs)

    written = write_matches_csv(matches, Path(output))
    matched_patients = len({m["mrn"] for m in matches})
    us_patients = len({e["mrn"] for e in us_exams})
    logger.info(
        "Matched %d ultrasound-CT pair(s) across %d patient(s) "
        "(of %d with an ultrasound), CT within %.6gh and strictly after. "
        "Written to %s.",
        len(matches),
        matched_patients,
        us_patients,
        max_hours,
        written,
    )
    if not matches:
        logger.warning(
            "No pairs matched. Check that both CSVs cover overlapping dates "
            "and that their MRNs come from the same source."
        )


def cli() -> None:
    """CLI entry point."""
    fire.Fire(match)


if __name__ == "__main__":
    cli()
