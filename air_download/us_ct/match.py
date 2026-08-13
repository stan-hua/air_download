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

Where several ultrasounds precede one CT, keep only the one nearest it::

    pixi run match --us_csv us/accessions.csv --ct_csv ct/accessions.csv \
                   --us_selection closest
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

# How to pick one ultrasound when several precede the same CT. "all" keeps
# every candidate, which is the historical behaviour.
US_SELECTION_STRATEGIES = ("all", "closest")
DEFAULT_US_SELECTION = "all"

MATCH_CSV_HEADER = [
    "mrn",
    "us_accession_number",
    "us_date_time",
    "us_description",
    "us_image_count",
    "ct_accession_number",
    "ct_date_time",
    "ct_description",
    "ct_image_count",
    "hours_between",
    "n_preceding_us",
    "us_rank_before_ct",
    "is_closest_us",
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

    More than one ultrasound can precede the same CT inside the window, which
    would otherwise show up only as a repeated CT accession number. Each row
    therefore carries ``n_preceding_us`` (how many ultrasounds qualify for
    that CT, counted over every ultrasound in the input rather than only the
    ones this pairing emitted), ``us_rank_before_ct`` (1 for the earliest),
    and ``is_closest_us`` for the one immediately before the CT.

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
                # Count over every ultrasound for this patient, not just the
                # ones that were paired, so the ambiguity is reported even
                # when a preceding ultrasound was paired to a different CT.
                preceding = [
                    u
                    for u in patient_us
                    if 0 < _hours_between(u["when"], ct["when"]) <= max_hours
                ]
                rank = next(
                    i for i, u in enumerate(preceding) if u is us
                ) + 1
                matches.append(
                    {
                        "mrn": mrn,
                        "us_accession_number": us.get("accession_number", ""),
                        "us_date_time": us.get("date_time", ""),
                        "us_description": us.get("description", ""),
                        "us_image_count": us.get("image_count", ""),
                        "ct_accession_number": ct.get("accession_number", ""),
                        "ct_date_time": ct.get("date_time", ""),
                        "ct_description": ct.get("description", ""),
                        "ct_image_count": ct.get("image_count", ""),
                        "hours_between": round(
                            _hours_between(us["when"], ct["when"]), 3
                        ),
                        "n_preceding_us": len(preceding),
                        "us_rank_before_ct": rank,
                        "is_closest_us": rank == len(preceding),
                    }
                )
    return matches


def select_one_us_per_ct(
    matches: list[dict[str, Any]],
    strategy: str = DEFAULT_US_SELECTION,
) -> list[dict[str, Any]]:
    """Reduce each CT to a single ultrasound, by the requested strategy.

    ``match_exams`` is driven by ultrasounds, so a CT preceded by several of
    them appears on several rows. This narrows each ``(MRN, CT)`` group to one.

    ``closest`` ranks within each group rather than filtering on
    ``is_closest_us``. That flag is computed over every ultrasound the patient
    has, not only the ones emitted for this CT, so filtering on it couples the
    result to an invariant this function does not control; taking the highest
    rank present guarantees exactly one survivor per CT regardless.

    Timing is the only signal available here. Ranking on ``image_count`` was
    tried and removed: it counts DICOM objects rather than frames, so a
    single-view study saved as many stills outranks a multi-clip FAST. Frame
    counts settle that, and they exist only after downloading -- see
    ``air_frames``.

    Parameters
    ----------
    matches : list of dict
        Rows from :func:`match_exams`.
    strategy : str, optional
        One of :data:`US_SELECTION_STRATEGIES`. ``all`` returns the rows
        untouched.

    Returns
    -------
    list of dict
        The kept rows, in their original order.

    Raises
    ------
    ValueError
        If ``strategy`` is not a recognised value.
    """
    if strategy not in US_SELECTION_STRATEGIES:
        raise ValueError(
            f"Unknown us_selection '{strategy}'. Choose one of: "
            f"{', '.join(US_SELECTION_STRATEGIES)}."
        )
    if strategy == "all":
        return matches

    groups: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, row in enumerate(matches):
        groups[(row["mrn"], row["ct_accession_number"])].append((index, row))

    keep = {
        max(group, key=lambda item: item[1]["us_rank_before_ct"])[0]
        for group in groups.values()
    }
    return [row for index, row in enumerate(matches) if index in keep]


def count_ambiguous_cts(matches: list[dict[str, Any]]) -> int:
    """Count CT exams preceded by more than one ultrasound.

    Parameters
    ----------
    matches : list of dict
        Rows from :func:`match_exams`.

    Returns
    -------
    int
        Number of distinct CT exams with more than one qualifying
        ultrasound before them.
    """
    return len(
        {
            (m["mrn"], m["ct_accession_number"])
            for m in matches
            if m["n_preceding_us"] > 1
        }
    )


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
    us_selection: str = DEFAULT_US_SELECTION,
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
    us_selection : str, optional
        How to resolve a CT preceded by several ultrasounds: ``all`` (the
        default) keeps every candidate, ``closest`` keeps the one nearest the
        CT.
    verbose : bool, optional
        Log at DEBUG level.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if us_selection not in US_SELECTION_STRATEGIES:
        # Fail before reading two potentially large CSVs.
        raise ValueError(
            f"Unknown us_selection '{us_selection}'. Choose one of: "
            f"{', '.join(US_SELECTION_STRATEGIES)}."
        )

    us_exams = read_exams(Path(us_csv))
    ct_exams = read_exams(Path(ct_csv))
    matches = match_exams(us_exams, ct_exams, max_hours, all_pairs)

    # Measured before narrowing, so the report describes the data rather than
    # whatever the strategy happened to keep.
    ambiguous = count_ambiguous_cts(matches)
    selected = select_one_us_per_ct(matches, us_selection)
    dropped = len(matches) - len(selected)

    written = write_matches_csv(selected, Path(output))
    matched_patients = len({m["mrn"] for m in selected})
    us_patients = len({e["mrn"] for e in us_exams})
    logger.info(
        "Matched %d ultrasound-CT pair(s) across %d patient(s) "
        "(of %d with an ultrasound), CT within %.6gh and strictly after. "
        "Written to %s.",
        len(selected),
        matched_patients,
        us_patients,
        max_hours,
        written,
    )
    if dropped:
        logger.info(
            "Kept one ultrasound per CT by '%s', dropping %d of %d candidate "
            "row(s).",
            us_selection,
            dropped,
            len(matches),
        )
    if ambiguous and us_selection == "all":
        logger.warning(
            "%d CT exam(s) had more than one ultrasound before them inside "
            "the window, so those CTs appear on several rows. Filter on "
            "is_closest_us to keep one ultrasound per CT, pass "
            "--us_selection, or inspect n_preceding_us and us_rank_before_ct "
            "to decide case by case.",
            ambiguous,
        )
    elif ambiguous:
        logger.info(
            "%d CT exam(s) had more than one ultrasound before them inside "
            "the window; '%s' chose between them.",
            ambiguous,
            us_selection,
        )
    if not selected:
        logger.warning(
            "No pairs matched. Check that both CSVs cover overlapping dates "
            "and that their MRNs come from the same source."
        )


def cli() -> None:
    """CLI entry point."""
    fire.Fire(match)


if __name__ == "__main__":
    cli()
