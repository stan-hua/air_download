"""
frames.py

Description: Count the frames in downloaded DICOM files, and prune an
             archive down to the multi-frame clips worth keeping.

Frame counts are the one thing the AIR API cannot tell you. Its
``imageCount`` counts DICOM *objects* -- one ultrasound cine clip is a single
object whether it holds 2 frames or 200 -- and there is no frame, thickness,
or plane field anywhere in the API. ``NumberOfFrames`` (0028,0008) lives in
the file header, so it can only be read after downloading.

That makes this a two-pass workflow: download, then ``inspect`` to see the
frame distribution, then ``prune`` once you have settled on a threshold.
Reading stops before the pixel data, so inspecting is far cheaper than the
download that produced the files.

Neither command modifies its input. ``prune`` writes a new tree and leaves
the source archives untouched.

Examples
--------
Count frames across a downloaded cohort, writing frames.csv and
frames_exams.csv::

    pixi run frames inspect --input output-cohort/ --output frames.csv

Look at the distribution before committing to a cut-off::

    pixi run frames inspect --input output-cohort/ --min_frames 30

Keep only the clips of 60 frames or more, in a new tree::

    pixi run frames prune --input output-cohort/ --output_dir output-60frames/ \
        --min_frames 60
"""

# Standard libraries
import csv
import io
import logging
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

# Non-standard libraries
import fire
from pydicom import dcmread
from pydicom.errors import InvalidDicomError
from tqdm import tqdm

# Custom libraries
from air_download.utils import configure_logging

logger = logging.getLogger(__name__)

# A cine clip worth keeping, for an ED FAST. Stills come back as 1 frame.
DEFAULT_MIN_FRAMES = 60

FRAME_CSV_HEADER = [
    "archive",
    "member",
    "mrn",
    "accession_number",
    "series_uid",
    "series_description",
    "modality",
    "sop_instance_uid",
    "n_frames",
    "rows",
    "columns",
    "passes",
]

EXAM_CSV_HEADER = [
    "archive",
    "mrn",
    "accession_number",
    "n_instances",
    "n_passing",
    "max_frames",
    "total_frames",
]

# Members that are not image instances. An includeLog download adds a
# spreadsheet, and a DICOMDIR is an index rather than an image.
_SKIP_MEMBERS = {"DICOMDIR"}
_SKIP_SUFFIXES = {".xls", ".xlsx", ".csv", ".txt", ".log", ".json", ".pdf"}


def _exam_csv_path(output: Path) -> Path:
    """Return the per-exam companion path for a per-instance CSV."""
    output = Path(output)
    return output.with_name(f"{output.stem}_exams{output.suffix or '.csv'}")


def _is_skippable(name: str) -> bool:
    """Report whether an archive member is obviously not a DICOM instance."""
    path = Path(name)
    if name.endswith("/"):
        return True
    return path.name in _SKIP_MEMBERS or path.suffix.lower() in _SKIP_SUFFIXES


def _read_header(data: bytes) -> Any | None:
    """Parse a DICOM header, returning None when the bytes are not DICOM."""
    try:
        # stop_before_pixels keeps this to the header: no pixel decoding, and
        # a fraction of the work a full read would do.
        return dcmread(io.BytesIO(data), stop_before_pixels=True, force=False)
    except (InvalidDicomError, AttributeError, ValueError, EOFError):
        return None


def iter_instances(root: Path) -> Iterator[tuple[str, str, Any]]:
    """Yield ``(archive, member, dataset)`` for every DICOM instance under root.

    Handles both shapes this package produces: ``.zip`` archives as
    downloaded, and loose files already extracted. For a loose file the
    "archive" is its parent directory, so an exam stays grouped either way.

    Parameters
    ----------
    root : Path
        A directory to walk, or a single ``.zip``.

    Yields
    ------
    tuple
        The archive label, the member name within it, and the parsed header.
    """
    root = Path(root)
    archives = [root] if root.is_file() else sorted(root.rglob("*.zip"))

    loose: list[Path] = []
    if root.is_dir() and not archives:
        loose = sorted(p for p in root.rglob("*") if p.is_file())

    unreadable = 0

    for archive in tqdm(archives, desc="Reading archives", disable=not archives):
        label = str(archive.relative_to(root) if root.is_dir() else archive.name)
        try:
            with zipfile.ZipFile(archive) as zf:
                for name in zf.namelist():
                    if _is_skippable(name):
                        continue
                    dataset = _read_header(zf.read(name))
                    if dataset is None:
                        unreadable += 1
                        continue
                    yield label, name, dataset
        except (zipfile.BadZipFile, OSError):
            logger.error(
                "An archive could not be opened and was skipped; re-download "
                "it to include it."
            )

    for path in tqdm(loose, desc="Reading files", disable=not loose):
        if _is_skippable(path.name):
            continue
        dataset = _read_header(path.read_bytes())
        if dataset is None:
            unreadable += 1
            continue
        yield str(path.parent.relative_to(root)), path.name, dataset

    if unreadable:
        logger.info(
            "Skipped %d file(s) that are not DICOM instances (logs, indexes).",
            unreadable,
        )


def _frame_count(dataset: Any) -> int:
    """Return an instance's frame count, treating a single-frame image as 1."""
    try:
        return max(1, int(getattr(dataset, "NumberOfFrames", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _instance_row(
    archive: str, member: str, dataset: Any, min_frames: int
) -> dict[str, Any]:
    """Build one CSV row describing a DICOM instance."""
    n_frames = _frame_count(dataset)
    # Fall back to the archive's stem: an anonymization profile may have
    # stripped the accession number out of the header.
    accession = str(getattr(dataset, "AccessionNumber", "") or "") or Path(
        archive
    ).stem
    return {
        "archive": archive,
        "member": member,
        "mrn": str(getattr(dataset, "PatientID", "") or ""),
        "accession_number": accession,
        "series_uid": str(getattr(dataset, "SeriesInstanceUID", "") or ""),
        "series_description": str(getattr(dataset, "SeriesDescription", "") or ""),
        "modality": str(getattr(dataset, "Modality", "") or ""),
        "sop_instance_uid": str(getattr(dataset, "SOPInstanceUID", "") or ""),
        "n_frames": n_frames,
        "rows": getattr(dataset, "Rows", "") or "",
        "columns": getattr(dataset, "Columns", "") or "",
        "passes": n_frames >= min_frames,
    }


def summarise_by_exam(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll per-instance rows up to one row per archive.

    Parameters
    ----------
    rows : list of dict
        Rows keyed by :data:`FRAME_CSV_HEADER`.

    Returns
    -------
    list of dict
        One row per archive, keyed by :data:`EXAM_CSV_HEADER`, in the order
        the archives were first seen.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["archive"]].append(row)

    summaries = []
    for archive, instances in grouped.items():
        frames = [r["n_frames"] for r in instances]
        summaries.append(
            {
                "archive": archive,
                "mrn": next((r["mrn"] for r in instances if r["mrn"]), ""),
                "accession_number": next(
                    (r["accession_number"] for r in instances if r["accession_number"]),
                    "",
                ),
                "n_instances": len(instances),
                "n_passing": sum(1 for r in instances if r["passes"]),
                "max_frames": max(frames),
                "total_frames": sum(frames),
            }
        )
    return summaries


def _write_csv(rows: list[dict[str, Any]], output: Path, header: list[str]) -> Path:
    """Write rows to a CSV, overwriting any existing file."""
    output = Path(output)
    if output.parent != Path(""):
        output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
    return output


def inspect(
    input: str | Path,
    output: str | Path = "frames.csv",
    min_frames: int = DEFAULT_MIN_FRAMES,
    verbose: bool = False,
    quiet: bool = False,
) -> None:
    """Count the frames in every downloaded DICOM instance.

    Writes two CSVs and changes nothing on disk otherwise: ``output`` holds
    one row per instance, and a companion ``<output>_exams.csv`` rolls that
    up per archive so exams with no qualifying clip are easy to spot.

    ``min_frames`` only fills in the ``passes`` column and the summary
    counts; every instance is reported either way, so you can look at the
    distribution before settling on a threshold.

    Parameters
    ----------
    input : str or Path
        Directory of downloaded archives (or extracted files), or one ``.zip``.
    output : str or Path, optional
        Where to write the per-instance CSV.
    min_frames : int, optional
        Frame count at or above which an instance counts as a clip worth
        keeping.
    verbose : bool, optional
        Log at DEBUG.
    quiet : bool, optional
        Log at ERROR only.

    Raises
    ------
    ValueError
        If ``min_frames`` is less than 1.
    FileNotFoundError
        If ``input`` does not exist.
    """
    configure_logging(verbose, quiet)
    if min_frames < 1:
        raise ValueError(f"min_frames must be at least 1, got {min_frames}.")
    root = Path(input)
    if not root.exists():
        raise FileNotFoundError(f"{root} does not exist.")

    rows = [
        _instance_row(archive, member, dataset, min_frames)
        for archive, member, dataset in iter_instances(root)
    ]

    if not rows:
        logger.warning("No DICOM instances found under %s; nothing to report.", root)
        return

    summaries = summarise_by_exam(rows)
    written = _write_csv(rows, Path(output), FRAME_CSV_HEADER)
    exams_written = _write_csv(
        summaries, _exam_csv_path(Path(output)), EXAM_CSV_HEADER
    )

    passing = sum(1 for r in rows if r["passes"])
    empty = [s for s in summaries if s["n_passing"] == 0]
    logger.info(
        "Read %d instance(s) across %d archive(s). %d have >=%d frames. "
        "Written to %s and %s.",
        len(rows),
        len(summaries),
        passing,
        min_frames,
        written,
        exams_written,
    )
    if empty:
        # Counts only here; the archives themselves are named in the CSV,
        # since an accession number is an identifier.
        logger.warning(
            "%d of %d archive(s) have no instance with >=%d frames, so they "
            "would be dropped entirely. They are the rows with n_passing=0 "
            "in %s.",
            len(empty),
            len(summaries),
            min_frames,
            exams_written,
        )


def prune(
    input: str | Path,
    output_dir: str | Path,
    min_frames: int = DEFAULT_MIN_FRAMES,
    verbose: bool = False,
    quiet: bool = False,
) -> None:
    """Copy archives into a new tree, keeping only the multi-frame clips.

    The source is never modified: each archive is rewritten under
    ``output_dir`` at the same relative path, holding only the instances with
    at least ``min_frames`` frames. An archive left with nothing is not
    written at all, and is counted in a warning.

    Parameters
    ----------
    input : str or Path
        Directory of downloaded archives, or one ``.zip``.
    output_dir : str or Path
        Root of the new tree. Must not be inside ``input``.
    min_frames : int, optional
        Keep instances with at least this many frames.
    verbose : bool, optional
        Log at DEBUG.
    quiet : bool, optional
        Log at ERROR only.

    Raises
    ------
    ValueError
        If ``min_frames`` is less than 1, or ``output_dir`` sits inside
        ``input``, which would make the run read what it is writing.
    FileNotFoundError
        If ``input`` does not exist.
    """
    configure_logging(verbose, quiet)
    if min_frames < 1:
        raise ValueError(f"min_frames must be at least 1, got {min_frames}.")
    root = Path(input)
    if not root.exists():
        raise FileNotFoundError(f"{root} does not exist.")

    destination = Path(output_dir)
    if root.is_dir() and destination.resolve().is_relative_to(root.resolve()):
        raise ValueError(
            f"output_dir ({destination}) is inside input ({root}); write the "
            f"pruned tree somewhere else so the run cannot read its own output."
        )

    keep: dict[str, set[str]] = defaultdict(set)
    dropped = 0
    for archive, member, dataset in iter_instances(root):
        if _frame_count(dataset) >= min_frames:
            keep[archive].add(member)
        else:
            dropped += 1

    archives = [root] if root.is_file() else sorted(root.rglob("*.zip"))
    written = 0
    emptied = 0
    for archive in tqdm(archives, desc="Writing pruned archives"):
        label = str(archive.relative_to(root) if root.is_dir() else archive.name)
        members = keep.get(label)
        if not members:
            emptied += 1
            continue
        target = destination / label
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(
            target, "w", zipfile.ZIP_DEFLATED
        ) as out:
            for name in source.namelist():
                if name in members:
                    out.writestr(name, source.read(name))
        written += 1

    logger.info(
        "Wrote %d pruned archive(s) to %s, keeping instances with >=%d "
        "frames and dropping %d. The source tree is unchanged.",
        written,
        destination,
        min_frames,
        dropped,
    )
    if emptied:
        logger.warning(
            "%d archive(s) had no instance with >=%d frames and were not "
            "written. Run 'inspect' to see which, by n_passing=0.",
            emptied,
            min_frames,
        )


def cli() -> None:
    """CLI entry point."""
    fire.Fire({"inspect": inspect, "prune": prune})


if __name__ == "__main__":
    cli()
