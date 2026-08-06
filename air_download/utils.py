"""Utility functions for date ranges, output path handling, and CSV writing."""

import csv
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The data source caps how many exams a single query may return, so long
# date ranges are split into windows of at most this many days.
DEFAULT_CHUNK_DAYS = 7

_CSV_HEADER = [
    "mrn",
    "accession_number",
    "date_time",
    "sex",
    "birthdate",
    "description",
    "image_count",
]


def parse_datetime(value: str) -> datetime:
    """Parse a user-supplied date or datetime into a timezone-aware value.

    Accepts any ISO 8601 form ``datetime.fromisoformat`` understands, e.g.
    ``2024-01-15``, ``2024-01-15T13:30:00``, or ``2024-01-15T13:30:00-08:00``.
    A trailing ``Z`` is accepted. Naive values are interpreted in the local
    timezone, because the API expects an offset.

    Args:
        value: The date or datetime string to parse.

    Returns:
        A timezone-aware datetime.

    Raises:
        ValueError: If the value is not a recognizable ISO 8601 date/datetime.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"Could not parse date '{value}'. Use an ISO 8601 date or "
            f"datetime, e.g. 2024-01-15 or 2024-01-15T13:30:00-08:00."
        ) from exc
    # Attach the local offset; the API's accepted datetime formats all
    # carry one (only the bare yyyy-MM-dd form may omit it).
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def build_date_ranges(
    date_start: str | None,
    date_end: str | None,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Build the ``dateRange`` payloads covering the requested window.

    A window longer than ``chunk_days`` is split into consecutive chunks of
    at most that length, so each query stays under the data source's result
    cap. Chunk boundaries touch, so an exam sitting exactly on one can come
    back from two queries; callers must de-duplicate.

    Args:
        date_start: Start of the window (ISO 8601), or None for no lower
            bound.
        date_end: End of the window (ISO 8601). Defaults to the current time
            when ``date_start`` is given.
        chunk_days: Maximum length of a single chunk, in days.
        now: Current time, injectable for testing. Defaults to
            ``datetime.now()`` in the local timezone.

    Returns:
        A list of ``dateRange`` dictionaries, each with ``start``, ``end``,
        and ``label`` keys. Always at least one element; a single element
        with empty strings means "no date restriction".

    Raises:
        ValueError: If ``chunk_days`` is not positive, or if the window ends
            before it starts.
    """
    if chunk_days < 1:
        raise ValueError(f"chunk_days must be at least 1, got {chunk_days}.")

    start = parse_datetime(date_start) if date_start else None
    if date_end:
        end = parse_datetime(date_end)
    elif start is not None:
        # An open-ended window starting in the past means "up to now".
        end = now if now is not None else datetime.now().astimezone()
    else:
        end = None

    if start is None:
        # Nothing to chunk: no lower bound to walk forward from.
        return [{"start": "", "end": end.isoformat(timespec="seconds") if end else "", "label": ""}]

    if end < start:
        raise ValueError(
            f"Date range ends before it starts: {start.isoformat()} to "
            f"{end.isoformat()}."
        )

    step = timedelta(days=chunk_days)
    ranges = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + step, end)
        ranges.append(
            {
                "start": chunk_start.isoformat(timespec="seconds"),
                "end": chunk_end.isoformat(timespec="seconds"),
                "label": "",
            }
        )
        chunk_start = chunk_end
    if not ranges:
        # start == end: a single instant is still a valid query.
        ranges.append(
            {
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                "label": "",
            }
        )
    return ranges


def exam_key(exam: dict[str, Any]) -> str | tuple[str, str]:
    """Return a value identifying an exam, for de-duplicating chunked results.

    Args:
        exam: An exam dictionary from the API.

    Returns:
        The study UID when present, otherwise the accession number paired
        with the exam date/time.
    """
    return exam.get("studyUid") or (
        exam.get("accessionNumber", ""),
        exam.get("dateTime", ""),
    )


def build_exam_output_path(
    base_output: Path | None, exam: dict[str, Any], exam_index: int
) -> Path:
    """Generate a unique output path for each exam.

    Handles three cases:

    - ``base_output`` is a directory (or has no ``.zip`` extension): creates
      ``base_output / <accessionNumber>.zip``
    - ``base_output`` is a ``.zip`` path that doesn't exist: returns it as-is
    - ``base_output`` is a ``.zip`` path that exists: appends index to avoid
      overwriting

    Args:
        base_output: The user-provided output path. If None, defaults to
            current directory.
        exam: The exam object from the API.
        exam_index: Index of the current exam in the loop.

    Returns:
        The resolved output path for this exam.
    """
    p = base_output if base_output is not None else Path(".")
    if p.suffix.lower() != ".zip":
        # p is supposed to be a directory
        p.mkdir(parents=True, exist_ok=True)
        acc_num = exam.get("accessionNumber") or f"exam_{exam_index + 1}"
        return p / f"{acc_num}.zip"
    elif not p.exists():
        return p
    else:
        # User provided a filename; append index to avoid overwriting
        return p.with_name(f"{p.stem}_{exam_index + 1}{p.suffix}")


def write_exams_csv(
    exams: list[dict[str, Any]], output_dir: Path, mrn: str | None = None
) -> Path:
    """Write exam search results to a CSV file.

    Appends to the file if it already exists. Writes a header row only if
    the file is new. The MRN column is populated from the user-provided
    ``mrn`` argument; if not given, falls back to ``patientId`` from each
    exam object (returned by the API when searching by accession).

    Args:
        exams: List of exam dictionaries from the API.
        output_dir: Directory where ``accessions.csv`` will be written.
        mrn: Patient MRN to include in each row. If None, the ``patientId``
            field from each exam is used instead.

    Returns:
        Path to the written CSV file.
    """
    output_csv = output_dir / "accessions.csv"
    file_exists = output_csv.exists()
    logger.info("Writing accessions to %s", output_csv)

    with open(output_csv, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(_CSV_HEADER)
        for exam in exams:
            writer.writerow(
                [
                    mrn or exam.get("patientId", ""),
                    exam.get("accessionNumber", ""),
                    exam.get("dateTime", ""),
                    exam.get("sex", ""),
                    exam.get("birthdate", ""),
                    exam.get("description", ""),
                    exam.get("imageCount", ""),
                ]
            )

    logger.info("Accessions written to file.")
    return output_csv
