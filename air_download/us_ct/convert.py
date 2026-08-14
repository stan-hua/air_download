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

The ultrasound half also de-identifies. Ultrasound burns patient details into
the banner around the image, and ``BurnedInAnnotation`` (0028,0301) is optional
and routinely absent, so it cannot be used to decide whether that happened.
Cropping to ``SequenceOfUltrasoundRegions`` -- the scanner's own statement of
which pixels are image -- removes it deterministically. That crop happens
first, and the ``ultraml`` beamform crop only ever runs inside it.

Examples
--------
Preview what would be written, touching nothing::

    pixi run convert ct --input output-cohort/ --output_dir output-arrays/ \
        --dry_run

Convert every CT in a cohort::

    pixi run convert ct --input output-cohort/ --output_dir output-arrays/

Convert the ultrasound clips, keeping those of 20 frames or more::

    pixi run convert us --input output-cohort/ --output_dir output-arrays/ \
        --min_frames 20
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
import zarr
from pydicom import dcmread
from pydicom.errors import InvalidDicomError
from tqdm import tqdm
from ultraml.core.extract import EmptyMaskError, compute_ultrasound_video_mask
from zarr.codecs import BloscCodec

# Custom libraries
from air_download.frames import DEFAULT_MIN_FRAMES, _is_skippable
from air_download.utils import configure_logging

logger = logging.getLogger(__name__)

CT_SUFFIX = ".nii.gz"
US_SUFFIX = ".zarr"

# Measured on real FAST clips. Chunking several frames together is what lets
# zstd find the redundancy *between* frames, which a one-frame chunk cannot:
# one frame per chunk holds 38% of raw, eight frames at clevel 9 holds 22%.
# The gain needs both -- clevel 9 alone, at one frame per chunk, only reaches
# 35%, because there is too little in a chunk to find. Eight frames still
# reads a single frame in ~3 ms, and a model fed a window of consecutive
# frames reads a whole chunk anyway. Bitshuffle was measured and dropped: on
# 8-bit speckle it is very slightly larger and slower to read.
DEFAULT_CHUNK_FRAMES = 8
US_COMPRESSOR = BloscCodec(cname="zstd", clevel=9, shuffle="noshuffle")

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


def region_box(dataset: Any) -> tuple[int, int, int, int]:
    """Return the scanner's own ultrasound region, as ``(x0, y0, x1, y1)``.

    ``SequenceOfUltrasoundRegions`` (0018,6011) is where the scanner declares
    which part of the frame is image rather than chrome. Cropping to it is the
    de-identification step: ultrasound burns patient details into the banner
    outside it, and ``BurnedInAnnotation`` (0028,0301) is optional and
    routinely absent, so it cannot be used to decide whether that happened.

    Parameters
    ----------
    dataset : pydicom.Dataset
        A parsed ultrasound instance.

    Returns
    -------
    tuple of int
        The region's bounds within the frame.

    Raises
    ------
    ConversionError
        If the instance declares no region. Falling back to the whole frame
        would write the banner, so this refuses instead.
    """
    regions = getattr(dataset, "SequenceOfUltrasoundRegions", None) or []
    if not regions:
        raise ConversionError(
            "This instance declares no SequenceOfUltrasoundRegions, so the "
            "image area cannot be identified and the banner -- which may "
            "carry burned-in patient details -- cannot be cropped away."
        )
    region = regions[0]
    return (
        int(region.RegionLocationMinX0),
        int(region.RegionLocationMinY0),
        int(region.RegionLocationMaxX1),
        int(region.RegionLocationMaxY1),
    )


def is_grayscale(frames: np.ndarray, tolerance: int = 20) -> bool:
    """Report whether a colour clip is really grayscale in an RGB container.

    B-mode ultrasound is grayscale but is often stored with three identical
    channels, so collapsing costs nothing and saves two thirds of the space.
    Colour Doppler is the exception and must keep its channels. The test
    tolerates a little chroma noise, because the source is JPEG.

    Parameters
    ----------
    frames : numpy.ndarray
        Clip of shape ``(T, H, W, C)``.
    tolerance : int, optional
        Channel spread at or below which a pixel counts as gray.

    Returns
    -------
    bool
        True when the clip carries no meaningful colour.
    """
    if frames.ndim != 4 or frames.shape[-1] < 3:
        return True
    spread = frames.max(axis=-1).astype(np.int16) - frames.min(axis=-1)
    return float((spread > tolerance).mean()) <= 0.001


def tighten_to_beamform(frames: np.ndarray) -> tuple[np.ndarray, tuple | None]:
    """Crop a clip to the moving beamform, or leave it as it is.

    The region box is conservative -- it keeps depth markers and the ``cm``
    label in the corner. ``ultraml`` finds the part that actually moves. This
    runs *after* the region crop, never instead of it: the mask grows through
    connected bright pixels, so on a full frame it could in principle reach
    the banner, whereas after the crop there is no banner left to reach.

    Parameters
    ----------
    frames : numpy.ndarray
        Clip already cropped to its region box.

    Returns
    -------
    tuple
        The cropped clip, and the box used as ``(x0, y0, x1, y1)``, or None
        when no beamform was found and the clip was left alone.
    """
    try:
        _, (y_min, y_max, x_min, x_max) = compute_ultrasound_video_mask(frames)
    except EmptyMaskError:
        # Nothing moves. The region crop already made this safe, so keep the
        # clip rather than discarding data.
        return frames, None
    return frames[:, y_min:y_max, x_min:x_max], (x_min, y_min, x_max, y_max)


def convert_us_archive(
    archive: Path,
    destination: Path,
    min_frames: int = DEFAULT_MIN_FRAMES,
    tighten: bool = True,
    chunk_frames: int = DEFAULT_CHUNK_FRAMES,
) -> int:
    """Convert one ultrasound archive into a Zarr group, one array per clip.

    Each clip becomes ``clip-<member_index>``, numbered by position in the
    archive so it joins straight to the ``member_index`` column of
    ``frames.csv``. Arrays are chunked a few frames at a time, which compresses
    far better than one frame per chunk while still reading a single frame in
    milliseconds.

    Parameters
    ----------
    archive : Path
        A ``.zip`` holding one ultrasound exam.
    destination : Path
        Where to write the ``.zarr`` group.
    min_frames : int, optional
        Skip clips shorter than this. Stills and orphan fragments.
    tighten : bool, optional
        Crop to the moving beamform after the region crop.
    chunk_frames : int, optional
        Frames per chunk. Larger chunks compress better and read a window of
        consecutive frames faster; smaller chunks read one random frame faster.

    Returns
    -------
    int
        Number of clips written.
    """
    written = 0
    group = None

    with zipfile.ZipFile(archive) as zf:
        for index, name in enumerate(zf.namelist()):
            if _is_skippable(name):
                continue
            try:
                ds = dcmread(io.BytesIO(zf.read(name)), force=False)
            except (InvalidDicomError, AttributeError, ValueError, EOFError):
                continue
            if str(getattr(ds, "Modality", "")).upper() != "US":
                continue
            n_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
            if n_frames < min_frames:
                continue

            frames = np.asarray(ds.pixel_array)
            # A three-dimensional array is ambiguous -- (T, H, W) for a
            # grayscale clip, (H, W, C) for a colour still -- so the frame
            # count decides, never the shape.
            if n_frames == 1:
                frames = frames[None]

            x0, y0, x1, y1 = region_box(ds)
            frames = frames[:, y0:y1, x0:x1]

            tight = None
            if tighten:
                frames, tight = tighten_to_beamform(frames)

            gray = is_grayscale(frames)
            if gray and frames.ndim == 4:
                frames = frames[..., 0]

            frames = np.ascontiguousarray(frames, dtype=np.uint8)
            if group is None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                group = zarr.open_group(destination, mode="w")

            array = group.create_array(
                f"clip-{index:04d}",
                shape=frames.shape,
                dtype="uint8",
                chunks=(min(chunk_frames, frames.shape[0]), *frames.shape[1:]),
                compressors=US_COMPRESSOR,
            )
            array[:] = frames
            array.attrs.update(
                {
                    "member_index": index,
                    "n_frames": int(frames.shape[0]),
                    "grayscale": bool(gray),
                    "region_box": [x0, y0, x1, y1],
                    "beamform_box": list(tight) if tight else None,
                    "source_shape": [int(ds.Rows), int(ds.Columns)],
                }
            )
            written += 1

    if not written:
        raise ConversionError(
            f"No ultrasound clip of at least {min_frames} frames in this "
            f"archive."
        )
    return written


def us(
    input: str | Path,
    output_dir: str | Path,
    min_frames: int = DEFAULT_MIN_FRAMES,
    tighten: bool = True,
    chunk_frames: int = DEFAULT_CHUNK_FRAMES,
    dry_run: bool = False,
    verbose: bool = False,
    quiet: bool = False,
) -> None:
    """Convert every ultrasound archive in a cohort into Zarr clips.

    Three things happen to each clip, in this order. It is cropped to the
    scanner's declared region, which removes the banner and is the step that
    de-identifies the pixels. It is then tightened to the moving beamform,
    which removes the depth markers the region box keeps. Finally it is
    collapsed to one channel where the clip is grayscale in an RGB container,
    which is lossless and saves two thirds of the space -- colour Doppler
    keeps its channels.

    Note the default ``min_frames`` is shared with ``air_frames`` and is
    higher than the point where a cohort's frame distribution usually splits;
    inspect the distribution first and pass the threshold you settled on.

    Parameters
    ----------
    input : str or Path
        Root of a downloaded cohort.
    output_dir : str or Path
        Root of the new tree. Must not be inside ``input``.
    min_frames : int, optional
        Skip clips shorter than this.
    tighten : bool, optional
        Crop to the moving beamform after the region crop.
    chunk_frames : int, optional
        Frames per chunk in the written arrays.
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
        If ``output_dir`` sits inside ``input``, or ``min_frames`` is below 1.
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
            f"converted tree somewhere else so the run cannot read its own "
            f"output."
        )

    archives = sorted(root.rglob("us/*.zip")) if root.is_dir() else [root]
    if not archives:
        logger.warning("No ultrasound archives found under %s.", root)
        return

    clips = converted = failed = 0
    for archive in tqdm(archives, desc="Converting US", disable=dry_run):
        target = (destination / archive.relative_to(root)).with_suffix(US_SUFFIX)
        if dry_run:
            print(target)
            continue
        try:
            clips += convert_us_archive(
                archive, target, min_frames, tighten, chunk_frames
            )
            converted += 1
        except (ConversionError, zipfile.BadZipFile, OSError) as exc:
            failed += 1
            logger.error(
                "One ultrasound archive could not be converted (%s); "
                "continuing.",
                exc.__class__.__name__,
            )
            # Only at DEBUG: the detail can name the archive.
            logger.debug("Failure detail: %s", exc)

    if dry_run:
        logger.info("Dry run: %d archive(s) would be written.", len(archives))
        return
    logger.info(
        "Wrote %d clip(s) from %d archive(s) to %s, %d archive(s) failed.",
        clips,
        converted,
        destination,
        failed,
    )


def cli() -> None:
    """CLI entry point."""
    fire.Fire({"ct": ct, "us": us})


if __name__ == "__main__":
    cli()
