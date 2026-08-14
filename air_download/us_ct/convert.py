"""
convert.py

Description: Turn a downloaded ultrasound-CT cohort into arrays a PyTorch
             pipeline can load, one file per exam.

The two halves of a FAST-CT pair want different things. A CT arrives as a few
hundred single-frame objects that are slices of one volume, so conversion means
*assembling*: order the slices by where they sit in space, apply the rescale to
Hounsfield units, and write one NIfTI carrying the affine. An ultrasound
arrives as cine clips of different views, so conversion means *separating*:
one array per clip.

NIfTI is not a preference for the CT half, it is what the segmentation
ecosystem reads -- TotalSegmentator and the other open organ segmentors are
nnU-Net models, and they take NIfTI in and give NIfTI out. Storing the volumes
as anything else means converting to NIfTI to segment and back again.

The affine is the reason a bare array will not do. ``PixelSpacing`` varies from
patient to patient while slice thickness does not, so voxels are anisotropic
*and* inconsistent across a cohort; anything that resamples to a common spacing
needs the real millimetre geometry, and only the affine carries it.

Nothing here reads an identifier. Output mirrors the pseudonymous cohort
layout, so ``P0001/visit-01/ct/A0002.nii.gz`` sits where its archive did and
joins back through the crosswalk exactly as before.

Examples
--------
Preview what would be written, touching nothing::

    pixi run python -m air_download.us_ct.convert ct --input output-cohort/ \
        --output_dir output-nifti/ --dry_run

Convert every CT in a cohort::

    pixi run python -m air_download.us_ct.convert ct --input output-cohort/ \
        --output_dir output-nifti/
"""

# Standard libraries
import io
import logging
import zipfile
from pathlib import Path
from typing import Any

# Non-standard libraries
import fire
import nibabel as nib
import numpy as np
from pydicom import dcmread
from pydicom.errors import InvalidDicomError
from tqdm import tqdm

# Custom libraries
from air_download.frames import _is_skippable
from air_download.utils import configure_logging

logger = logging.getLogger(__name__)

CT_SUFFIX = ".nii.gz"

# Slice positions are floats off a scanner, so "same" needs a tolerance.
ORIENTATION_TOLERANCE = 1e-3
# A spacing that varies by more than this between slices is a gap, not noise.
SPACING_TOLERANCE = 0.01


class ConversionError(Exception):
    """A series cannot be assembled into a volume."""


def read_series(archive: Path, modality: str = "CT") -> list[Any]:
    """Read every instance of one modality out of an archive.

    Unlike :func:`air_download.frames.iter_instances` this reads pixel data,
    because the point is to assemble it.

    Parameters
    ----------
    archive : Path
        A ``.zip`` as downloaded.
    modality : str, optional
        Only instances of this modality are returned.

    Returns
    -------
    list
        The parsed datasets, in archive order.
    """
    datasets = []
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            if _is_skippable(name):
                continue
            try:
                ds = dcmread(io.BytesIO(zf.read(name)), force=False)
            except (InvalidDicomError, AttributeError, ValueError, EOFError):
                continue
            if str(getattr(ds, "Modality", "")).upper() == modality.upper():
                datasets.append(ds)
    return datasets


def slice_positions(datasets: list[Any]) -> np.ndarray:
    """Project each slice's origin onto the normal of the imaging plane.

    Sorting on this rather than ``InstanceNumber`` is the whole point:
    instance numbers are assigned by the scanner and are wrong often enough
    to silently produce a shuffled volume, whereas the projection is the
    slice's actual position along the stack.

    Parameters
    ----------
    datasets : list
        Slices of one series.

    Returns
    -------
    numpy.ndarray
        Position along the slice normal, in millimetres, one per slice.

    Raises
    ------
    ConversionError
        If a slice lacks its position or orientation.
    """
    try:
        orientation = np.asarray(datasets[0].ImageOrientationPatient, dtype=float)
        normal = np.cross(orientation[:3], orientation[3:])
        return np.array(
            [
                float(np.dot(np.asarray(ds.ImagePositionPatient, dtype=float), normal))
                for ds in datasets
            ]
        )
    except AttributeError as exc:
        raise ConversionError(
            "A slice is missing ImagePositionPatient or "
            "ImageOrientationPatient, so the stack cannot be ordered."
        ) from exc


def check_series(datasets: list[Any]) -> None:
    """Reject a series that cannot honestly be stacked into one volume.

    Parameters
    ----------
    datasets : list
        Slices of one series, already sorted.

    Raises
    ------
    ConversionError
        If the slices disagree on size or orientation, or if two occupy the
        same position.
    """
    if not datasets:
        raise ConversionError("No CT instances in this archive.")

    shapes = {(int(ds.Rows), int(ds.Columns)) for ds in datasets}
    if len(shapes) > 1:
        raise ConversionError(
            f"Slices disagree on size ({len(shapes)} distinct shapes); this "
            f"archive holds more than one acquisition."
        )

    reference = np.asarray(datasets[0].ImageOrientationPatient, dtype=float)
    for ds in datasets:
        other = np.asarray(ds.ImageOrientationPatient, dtype=float)
        if not np.allclose(reference, other, atol=ORIENTATION_TOLERANCE):
            raise ConversionError(
                "Slices disagree on ImageOrientationPatient, so they do not "
                "share an imaging plane."
            )

    positions = slice_positions(datasets)
    gaps = np.diff(positions)
    if len(gaps) and np.any(np.isclose(gaps, 0.0)):
        raise ConversionError(
            "Two or more slices occupy the same position along the stack; "
            "the archive holds duplicates."
        )


def slice_spacing(positions: np.ndarray) -> float:
    """Return the spacing between slices, warning when it is not uniform."""
    if len(positions) < 2:
        return 1.0
    gaps = np.diff(positions)
    spacing = float(np.median(gaps))
    if np.ptp(gaps) > SPACING_TOLERANCE:
        # Counts only: naming the exam would name an accession number.
        logger.warning(
            "Slice spacing varies across one series (%.3f to %.3f mm); the "
            "median is used and the volume is not geometrically exact.",
            float(gaps.min()),
            float(gaps.max()),
        )
    return spacing or 1.0


def build_affine(reference: Any, spacing: float) -> np.ndarray:
    """Build the voxel-to-world affine, converted from DICOM LPS to NIfTI RAS.

    Parameters
    ----------
    reference : pydicom.Dataset
        The first slice of the sorted stack.
    spacing : float
        Distance between slices, in millimetres.

    Returns
    -------
    numpy.ndarray
        A 4x4 affine mapping voxel indices ``(i, j, k)`` to RAS millimetres.
    """
    orientation = np.asarray(reference.ImageOrientationPatient, dtype=float)
    column_cosine, row_cosine = orientation[:3], orientation[3:]
    # PixelSpacing is [between rows, between columns] -- that is, the first
    # value is the step taken moving down a column, not across a row.
    row_spacing, column_spacing = (float(v) for v in reference.PixelSpacing)
    normal = np.cross(column_cosine, row_cosine)
    origin = np.asarray(reference.ImagePositionPatient, dtype=float)

    affine = np.eye(4)
    affine[:3, 0] = column_cosine * column_spacing
    affine[:3, 1] = row_cosine * row_spacing
    affine[:3, 2] = normal * spacing
    affine[:3, 3] = origin
    # DICOM is LPS, NIfTI is RAS: flip the first two world axes.
    return np.diag([-1.0, -1.0, 1.0, 1.0]) @ affine


def to_hounsfield(datasets: list[Any]) -> np.ndarray:
    """Stack slices into an int16 volume in Hounsfield units.

    int16 is deliberate. Hounsfield units are integers over roughly -1024 to
    3071, so float32 would double the size of every volume to store nothing.

    Parameters
    ----------
    datasets : list
        Slices of one series, already sorted.

    Returns
    -------
    numpy.ndarray
        Volume of shape ``(columns, rows, slices)``, matching the affine.
    """
    planes = []
    for ds in datasets:
        pixels = ds.pixel_array.astype(np.float32)
        slope = float(getattr(ds, "RescaleSlope", 1) or 1)
        intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
        planes.append(pixels * slope + intercept)

    volume = np.stack(planes, axis=-1)          # (rows, columns, slices)
    volume = np.clip(np.round(volume), -32768, 32767).astype(np.int16)
    # nibabel indexes (i, j, k) against the affine's columns, and the affine's
    # first column is the column direction -- so columns lead.
    return np.transpose(volume, (1, 0, 2))


def convert_ct_archive(archive: Path, destination: Path) -> Path:
    """Assemble one CT archive into a NIfTI volume.

    Parameters
    ----------
    archive : Path
        A ``.zip`` holding one CT series.
    destination : Path
        Where to write the ``.nii.gz``.

    Returns
    -------
    Path
        The written file.

    Raises
    ------
    ConversionError
        If the archive does not hold one stackable series.
    """
    datasets = read_series(archive, "CT")
    if not datasets:
        raise ConversionError("No CT instances in this archive.")

    order = np.argsort(slice_positions(datasets))
    datasets = [datasets[i] for i in order]
    check_series(datasets)

    spacing = slice_spacing(slice_positions(datasets))
    image = nib.Nifti1Image(to_hounsfield(datasets), build_affine(datasets[0], spacing))
    image.header.set_xyzt_units("mm")

    destination.parent.mkdir(parents=True, exist_ok=True)
    nib.save(image, destination)
    return destination


def ct(
    input: str | Path,
    output_dir: str | Path,
    dry_run: bool = False,
    verbose: bool = False,
    quiet: bool = False,
) -> None:
    """Convert every CT archive in a cohort into a NIfTI volume.

    Output mirrors the input layout, so ``P0001/visit-01/ct/A0002.zip``
    becomes ``P0001/visit-01/ct/A0002.nii.gz`` and still joins back through
    the crosswalk. An archive that cannot be assembled is counted and the run
    continues.

    Parameters
    ----------
    input : str or Path
        Root of a downloaded cohort.
    output_dir : str or Path
        Root of the new tree. Must not be inside ``input``.
    dry_run : bool, optional
        Report what would be written and write nothing.
    verbose : bool, optional
        Log at DEBUG.
    quiet : bool, optional
        Log at ERROR only.

    Raises
    ------
    FileNotFoundError
        If ``input`` does not exist.
    ValueError
        If ``output_dir`` sits inside ``input``.
    """
    configure_logging(verbose, quiet)
    root = Path(input)
    if not root.exists():
        raise FileNotFoundError(f"{root} does not exist.")

    destination = Path(output_dir)
    if root.is_dir() and destination.resolve().is_relative_to(root.resolve()):
        raise ValueError(
            f"output_dir ({destination}) is inside input ({root}); write the "
            f"converted tree somewhere else so the run cannot read its own "
            f"output."
        )

    archives = sorted(root.rglob("ct/*.zip")) if root.is_dir() else [root]
    if not archives:
        logger.warning("No CT archives found under %s; nothing to convert.", root)
        return

    written = failed = 0
    for archive in tqdm(archives, desc="Converting CT", disable=dry_run):
        target = (destination / archive.relative_to(root)).with_suffix("")
        target = target.with_name(target.name + CT_SUFFIX)
        if dry_run:
            print(target)
            continue
        try:
            convert_ct_archive(archive, target)
            written += 1
        except (ConversionError, zipfile.BadZipFile, OSError) as exc:
            failed += 1
            logger.error(
                "One CT archive could not be converted (%s); continuing.",
                exc.__class__.__name__,
            )
            # Only at DEBUG: the detail can name the archive.
            logger.debug("Failure detail: %s", exc)

    if dry_run:
        logger.info("Dry run: %d volume(s) would be written.", len(archives))
        return
    logger.info(
        "Wrote %d NIfTI volume(s) to %s, %d failed.", written, destination, failed
    )


def cli() -> None:
    """CLI entry point."""
    fire.Fire({"ct": ct})


if __name__ == "__main__":
    cli()
