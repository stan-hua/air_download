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

The threshold applies to ultrasound only, by default. One CT object is one
slice, so a threshold applied to a CT rejects every slice and takes the whole
series with it; ``min_frames_modalities`` names the modalities it means
anything for.

Identifiers in both CSVs are read from the **path**, not from the DICOM
header. ``PatientID`` and ``AccessionNumber`` hold the real values whenever a
download ran without an anonymization profile, so reading them would write
real identifiers into a file. Members are reported by index rather than by
name for the same reason: AIR names them ``<studyUid>/<seriesUid>/<sopUid>``,
which would reintroduce the UIDs this module deliberately stopped writing. A
cohort from ``air_cohort`` is already laid out as
``P0001/visit-01/us/A0001.zip``; join back to real patients through its
crosswalk. Anything outside that layout leaves the two columns empty rather
than guessing.

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
from air_download.crosswalk import parse_anon_ids
from air_download.utils import configure_logging

logger = logging.getLogger(__name__)

# A cine clip worth keeping, for an ED FAST. Stills come back as 1 frame.
DEFAULT_MIN_FRAMES = 60

# The frame threshold only applies to modalities where one object holds many
# frames. A CT is a stack of single-frame objects, so applying it there would
# reject every slice and take the whole series with it.
DEFAULT_MIN_FRAMES_MODALITIES = "US"

# Identifiers come from the path, never the DICOM header: PatientID and
# AccessionNumber hold the real values whenever no anonymization profile was
# applied, and writing them here would put them straight into a CSV. The
# UIDs are gone for the same reason -- they are keys back into the PACS.
FRAME_CSV_HEADER = [
    "archive",
    "member_index",
    "anon_mrn",
    "anon_accession_number",
    "series_description",
    "modality",
    "n_frames",
    "rows",
    "columns",
    "passes",
]

EXAM_CSV_HEADER = [
    "archive",
    "anon_mrn",
    "anon_accession_number",
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


def iter_instances(root: Path) -> Iterator[tuple[str, str, int, Any]]:
    """Yield ``(archive, member, member_index, dataset)`` for every instance.

    Handles both shapes this package produces: ``.zip`` archives as
    downloaded, and loose files already extracted. For a loose file the
    "archive" is its parent directory, so an exam stays grouped either way.

    ``member_index`` is the member's position in ``ZipFile.namelist()``, or
    for a loose tree its position in the parent directory's sorted listing.
    It counts skipped members too, so it stays a usable index back into the
    archive. Only the index reaches the CSV: an AIR download names members
    ``<studyUid>/<seriesUid>/<sopUid>.dcm``, so writing the name would put
    the very UIDs this module stopped reporting straight back into a column.

    Parameters
    ----------
    root : Path
        A directory to walk, or a single ``.zip``.

    Yields
    ------
    tuple
        The archive label, the member name, its index, and the parsed header.
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
                for index, name in enumerate(zf.namelist()):
                    if _is_skippable(name):
                        continue
                    dataset = _read_header(zf.read(name))
                    if dataset is None:
                        unreadable += 1
                        continue
                    yield label, name, index, dataset
        except (zipfile.BadZipFile, OSError):
            logger.error(
                "An archive could not be opened and was skipped; re-download "
                "it to include it."
            )

    # Position within the parent's sorted listing; `loose` is already sorted,
    # so a per-directory counter reproduces it.
    seen_in_dir: dict[Path, int] = {}
    for path in tqdm(loose, desc="Reading files", disable=not loose):
        index = seen_in_dir.get(path.parent, 0)
        seen_in_dir[path.parent] = index + 1
        if _is_skippable(path.name):
            continue
        dataset = _read_header(path.read_bytes())
        if dataset is None:
            unreadable += 1
            continue
        yield str(path.parent.relative_to(root)), path.name, index, dataset

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


def parse_modalities(value: str) -> frozenset[str]:
    """Parse a comma-separated modality list into an upper-cased set."""
    return frozenset(part.strip().upper() for part in value.split(",") if part.strip())


def instance_passes(
    dataset: Any, min_frames: int, modalities: frozenset[str]
) -> bool:
    """Report whether an instance clears the frame threshold.

    The threshold only means something where one object holds many frames.
    A CT is a stack of single-frame objects, so applying it there would
    reject every slice and drop the whole series -- which is why the check
    is limited to ``modalities`` and everything else passes untouched. An
    instance whose modality is missing passes too: never discard what cannot
    be classified.

    Parameters
    ----------
    dataset : pydicom.Dataset
        The parsed header.
    min_frames : int
        Frames at or above which an instance counts as a clip worth keeping.
    modalities : frozenset of str
        Modalities the threshold applies to. Empty means every modality.

    Returns
    -------
    bool
        True when the instance is kept.
    """
    if not modalities:
        return _frame_count(dataset) >= min_frames
    modality = str(getattr(dataset, "Modality", "") or "").upper()
    if modality not in modalities:
        return True
    return _frame_count(dataset) >= min_frames


def _instance_row(
    archive: str,
    member_index: int,
    dataset: Any,
    min_frames: int,
    modalities: frozenset[str],
) -> dict[str, Any]:
    """Build one CSV row describing a DICOM instance."""
    anon_mrn, anon_accession = parse_anon_ids(archive)
    return {
        "archive": archive,
        "member_index": member_index,
        "anon_mrn": anon_mrn,
        "anon_accession_number": anon_accession,
        "series_description": str(getattr(dataset, "SeriesDescription", "") or ""),
        "modality": str(getattr(dataset, "Modality", "") or ""),
        "n_frames": _frame_count(dataset),
        "rows": getattr(dataset, "Rows", "") or "",
        "columns": getattr(dataset, "Columns", "") or "",
        "passes": instance_passes(dataset, min_frames, modalities),
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
        # Derived from the path again rather than carried up from the rows,
        # so both CSVs answer to exactly one rule.
        anon_mrn, anon_accession = parse_anon_ids(archive)
        summaries.append(
            {
                "archive": archive,
                "anon_mrn": anon_mrn,
                "anon_accession_number": anon_accession,
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
    min_frames_modalities: str = DEFAULT_MIN_FRAMES_MODALITIES,
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
    min_frames_modalities : str, optional
        Comma-separated modalities the threshold applies to. Every other
        modality passes untouched. Pass an empty string to apply it to all.
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

    modalities = parse_modalities(min_frames_modalities)
    rows = [
        _instance_row(archive, index, dataset, min_frames, modalities)
        for archive, _member, index, dataset in iter_instances(root)
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
    unlabelled = [s for s in summaries if not s["anon_mrn"]]
    if unlabelled:
        logger.info(
            "%d of %d archive(s) are not in the pseudonymous cohort layout, "
            "so their anon_mrn and anon_accession_number are empty.",
            len(unlabelled),
            len(summaries),
        )
    exempt = sum(
        1 for r in rows if modalities and r["modality"].upper() not in modalities
    )
    logger.info(
        "Read %d instance(s) across %d archive(s). %d pass: >=%d frames, or "
        "any of the %d instance(s) the threshold does not apply to. Written "
        "to %s and %s.",
        len(rows),
        len(summaries),
        passing,
        min_frames,
        exempt,
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
    min_frames_modalities: str = DEFAULT_MIN_FRAMES_MODALITIES,
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
    min_frames_modalities : str, optional
        Comma-separated modalities the threshold applies to. Every other
        modality is kept whole -- without this a CT, being a stack of
        single-frame objects, would lose every slice. Empty applies it to all.
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

    modalities = parse_modalities(min_frames_modalities)
    keep: dict[str, set[str]] = defaultdict(set)
    dropped = 0
    for archive, member, _index, dataset in iter_instances(root):
        if instance_passes(dataset, min_frames, modalities):
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
