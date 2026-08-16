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
import cv2
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
from air_download.utils import (
    CT_ARRAY_SUFFIX,
    US_ARRAY_SUFFIX,
    configure_logging,
    converted_exam_path,
)

logger = logging.getLogger(__name__)

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

# Measured over 197 real FAST clips, projected to a 5,957-exam cohort: 8
# frames per chunk stores 138 GB in 159 files per exam, 32 frames stores
# 135 GB in 48. Size barely moves; the file count is the point, because that
# tree has to be copied to a GPU host. Past 32 the gain is another 1% and
# reading one random frame starts to cost a 64-frame decompress.
LETTERBOX_CHUNK_FRAMES = 32

# Slice positions are floats off a scanner, so "same" needs a tolerance.
ORIENTATION_TOLERANCE = 1e-3
# A spacing that varies by more than this between slices is a gap, not noise.
SPACING_TOLERANCE = 0.01


class ConversionError(Exception):
    """A series cannot be assembled into a volume."""


def _target_for(
    archive: Path, root: Path, destination: Path, suffix: str
) -> Path:
    """Where one archive's converted array goes.

    A cohort tree goes through :func:`converted_exam_path`, so this module
    and ``air_cohort`` resolve the same archive to the same path by calling
    the same code -- which is what makes ``--converted_dir`` a reliable
    resume signal rather than two rules that happen to agree today.

    A single archive passed directly is not in a cohort tree and has no
    modality folder to read, so the caller's suffix decides.
    """
    if root.is_dir():
        return converted_exam_path(archive, root, destination)
    return destination.with_name(destination.name + suffix)


def verify_ct_output(destination: Path, expected_shape: tuple[int, ...]) -> None:
    """Re-open a written volume and check it holds what was meant to go in.

    Nothing downstream re-reads these files before the source archive is
    deleted, so this is the only thing standing between a truncated write and
    an exam that would have to be pulled from the PACS again. It re-opens
    rather than trusting the writer's return: a short write, a full disk, and
    a half-flushed gzip stream all leave a file on disk that ``exists()``.

    Parameters
    ----------
    destination : Path
        The written ``.nii.gz``.
    expected_shape : tuple of int
        Shape the volume was built with.

    Raises
    ------
    ConversionError
        If the file cannot be re-opened or disagrees with ``expected_shape``.
    """
    try:
        reloaded = nib.load(destination)
        shape = tuple(int(v) for v in reloaded.shape)
    except Exception as exc:  # noqa: BLE001 - any failure to re-read is fatal
        raise ConversionError(
            f"The volume just written could not be re-opened "
            f"({exc.__class__.__name__}); treating the conversion as failed."
        ) from exc
    if shape != tuple(expected_shape):
        raise ConversionError(
            f"The volume just written has shape {shape}, but "
            f"{tuple(expected_shape)} was assembled; the file is truncated or "
            f"was written over."
        )


def verify_us_output(destination: Path, expected_clips: int) -> None:
    """Re-open a written Zarr group and check every clip reads back.

    Parameters
    ----------
    destination : Path
        The written ``.zarr`` group.
    expected_clips : int
        Number of clips the conversion reported writing.

    Raises
    ------
    ConversionError
        If the group cannot be re-opened, holds a different number of clips,
        or holds one whose first chunk will not decompress.
    """
    try:
        group = zarr.open_group(destination, mode="r")
        names = sorted(group.array_keys())
        for name in names:
            array = group[name]
            # Touch one chunk: the metadata can be intact while the chunk it
            # points at was never flushed.
            array[0]
    except Exception as exc:  # noqa: BLE001 - any failure to re-read is fatal
        raise ConversionError(
            f"The clips just written could not be re-opened "
            f"({exc.__class__.__name__}); treating the conversion as failed."
        ) from exc
    if len(names) != expected_clips:
        raise ConversionError(
            f"The group just written holds {len(names)} clip(s), but "
            f"{expected_clips} were written; the group is incomplete."
        )


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
    volume = to_hounsfield(datasets)
    image = nib.Nifti1Image(volume, build_affine(datasets[0], spacing))
    image.header.set_xyzt_units("mm")

    destination.parent.mkdir(parents=True, exist_ok=True)
    nib.save(image, destination)
    verify_ct_output(destination, volume.shape)
    return destination


def ct(
    input: str | Path,
    output_dir: str | Path,
    delete_source: bool = False,
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
    delete_source : bool, optional
        Delete each archive once its volume has been written *and* verified.
        This is what lets a long ingest run in batches without the source
        archives ever accumulating -- they are five times the size of what
        they convert into. Off by default: it is not reversible without
        another download.
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

    written = failed = deleted = 0
    for archive in tqdm(archives, desc="Converting CT", disable=dry_run):
        target = _target_for(archive, root, destination, CT_ARRAY_SUFFIX)
        if dry_run:
            print(target)
            continue
        try:
            convert_ct_archive(archive, target)
            written += 1
            if delete_source:
                # Only ever reached once verify_ct_output has re-opened the
                # volume, so the archive is redundant rather than the last
                # copy of anything.
                archive.unlink()
                deleted += 1
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
    if delete_source:
        logger.info(
            "Deleted %d source archive(s) whose volume was written and "
            "verified; %d left in place.",
            deleted,
            len(archives) - deleted,
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
    """Mask a clip down to the moving beamform and crop to it.

    The region box is conservative -- it keeps depth markers, the ``cm``
    label, and a small coloured vendor mark. ``ultraml`` finds the part that
    actually moves. This runs *after* the region crop, never instead of it:
    the mask grows through connected bright pixels, so on a full frame it
    could in principle reach the banner, whereas after the crop there is no
    banner left to reach.

    The mask is applied, not just its bounding box. Zeroing everything
    outside it removes the static chrome that sits within the box, halves the
    stored size, and is what makes a clip register as grayscale -- a 24x26
    coloured vendor mark is otherwise enough to force three channels on an
    entirely gray study. The dark pixels this discards are the background
    around the sector, not anechoic fluid inside it: filling the mask's
    interior holes first recovers almost none of them.

    Parameters
    ----------
    frames : numpy.ndarray
        Clip already cropped to its region box.

    Returns
    -------
    tuple
        The masked and cropped clip, and the box used as ``(x0, y0, x1, y1)``,
        or None when no beamform was found and the clip was left alone.
    """
    try:
        mask, (y_min, y_max, x_min, x_max) = compute_ultrasound_video_mask(frames)
    except EmptyMaskError:
        # Nothing moves. The region crop already made this safe, so keep the
        # clip rather than discarding data.
        return frames, None
    frames = frames.copy()
    frames[:, ~mask] = 0
    return frames[:, y_min:y_max, x_min:x_max], (x_min, y_min, x_max, y_max)


def letterbox_frames(
    frames: np.ndarray, size: int
) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Fit a clip into a square frame without distorting it.

    The beamform crop returns a tight box around the sector, and a sector is
    wider than it is deep -- measured over a real cohort the median clip is
    729x598, about 1.25:1, so a clip is not square and squashing it to a
    square stretches anatomy by a quarter. Scaling the long side to ``size``
    and zero-padding the short one keeps the geometry and is *cheaper*: the
    padding compresses to almost nothing while the pixels a squash invents do
    not. Measured at 256: 13.0 MB per exam letterboxed against 17.2 squashed.

    The padding is not a new artifact either. Everything outside the beamform
    is already zero from :func:`tighten_to_beamform`, so the bars continue the
    background rather than introducing an edge.

    A clip smaller than ``size`` is padded but never scaled up, since
    inventing detail to fill a frame costs space and buys nothing. The scale
    and offsets are returned so a caller can map back to source pixels.

    Parameters
    ----------
    frames : numpy.ndarray
        Clip of shape ``(T, H, W)``, or ``(T, H, W, C)`` for a colour clip.
    size : int
        Edge length of the square output.

    Returns
    -------
    tuple
        The letterboxed clip of shape ``(T, size, size)``, the scale factor
        applied, and the ``(y, x)`` offset of the content within the frame.
    """
    n_frames, height, width = frames.shape[:3]
    scale = min(1.0, size / max(height, width))
    target_h = max(1, min(size, round(height * scale)))
    target_w = max(1, min(size, round(width * scale)))

    out = np.zeros((n_frames, size, size, *frames.shape[3:]), dtype=np.uint8)
    y0, x0 = (size - target_h) // 2, (size - target_w) // 2
    for i in range(n_frames):
        # INTER_AREA is the correct filter for shrinking: it averages the
        # pixels that fall in each output cell instead of point-sampling
        # them, which on speckle is the difference between a smaller image
        # and a differently-noisy one.
        out[i, y0 : y0 + target_h, x0 : x0 + target_w] = cv2.resize(
            frames[i], (target_w, target_h), interpolation=cv2.INTER_AREA
        )
    return out, scale, (y0, x0)


def convert_us_archive(
    archive: Path,
    destination: Path,
    min_frames: int = DEFAULT_MIN_FRAMES,
    tighten: bool = True,
    chunk_frames: int = DEFAULT_CHUNK_FRAMES,
    grayscale: bool | None = None,
    letterbox: int | None = None,
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
    grayscale : bool, optional
        None decides per clip by looking for colour. True collapses every
        clip to one channel, which is what a B-mode-only cohort wants: it is
        a third of the size, and it cannot be defeated by a stray coloured
        pixel the mask happened to keep. False keeps every channel.
    letterbox : int, optional
        Fit every clip into a square frame of this edge length, preserving
        aspect. Requires ``grayscale``, since a colour clip has no time axis
        to letterbox along.

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

            gray = is_grayscale(frames) if grayscale is None else grayscale
            if gray and frames.ndim == 4:
                # Channels are identical, so any one of them is the picture.
                frames = frames[..., 0]

            scale, offset = 1.0, (0, 0)
            if letterbox:
                frames, scale, offset = letterbox_frames(frames, letterbox)

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
                    "grayscale_forced": grayscale is not None,
                    "region_box": [x0, y0, x1, y1],
                    "beamform_box": list(tight) if tight else None,
                    "source_shape": [int(ds.Rows), int(ds.Columns)],
                    # Enough to map a stored pixel back to the source frame,
                    # which the boxes alone no longer allow once a clip has
                    # been rescaled and centred in a larger frame.
                    "letterbox": int(letterbox) if letterbox else None,
                    "letterbox_scale": float(scale),
                    "letterbox_offset": list(offset),
                }
            )
            written += 1

    if not written:
        raise ConversionError(
            f"No ultrasound clip of at least {min_frames} frames in this "
            f"archive."
        )
    verify_us_output(destination, written)
    return written


def us(
    input: str | Path,
    output_dir: str | Path,
    min_frames: int = DEFAULT_MIN_FRAMES,
    tighten: bool = True,
    chunk_frames: int | None = None,
    grayscale: bool | None = None,
    letterbox: int | None = None,
    delete_source: bool = False,
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
        Frames per chunk in the written arrays. Defaults to 8 at native
        resolution and 32 when ``letterbox`` is set, since a letterboxed
        frame is a fraction of the size and a chunk should stay a few
        megabytes rather than a few hundred kilobytes.
    grayscale : bool, optional
        None decides per clip; True forces one channel for a cohort known to
        be B-mode only; False keeps every channel.
    letterbox : int, optional
        Fit every clip into a square frame of this edge length, preserving
        aspect and zero-padding the short side. Uniform shapes batch without
        a per-item resize, and 256 is a fifth of the size of native.
    delete_source : bool, optional
        Delete each archive once its clips have been written *and* verified.
        Off by default: it is not reversible without another download.
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
    if letterbox is not None and letterbox < 1:
        raise ValueError(f"letterbox must be at least 1, got {letterbox}.")
    if chunk_frames is None:
        chunk_frames = LETTERBOX_CHUNK_FRAMES if letterbox else DEFAULT_CHUNK_FRAMES
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

    clips = converted = failed = deleted = 0
    for archive in tqdm(archives, desc="Converting US", disable=dry_run):
        target = _target_for(archive, root, destination, US_ARRAY_SUFFIX)
        if dry_run:
            print(target)
            continue
        try:
            clips += convert_us_archive(
                archive,
                target,
                min_frames,
                tighten,
                chunk_frames,
                grayscale,
                letterbox,
            )
            converted += 1
            if delete_source:
                # Only ever reached once verify_us_output has re-opened every
                # clip, so the archive is redundant rather than the last copy.
                archive.unlink()
                deleted += 1
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
    if delete_source:
        logger.info(
            "Deleted %d source archive(s) whose clips were written and "
            "verified; %d left in place.",
            deleted,
            len(archives) - deleted,
        )


def cli() -> None:
    """CLI entry point."""
    fire.Fire({"ct": ct, "us": us})


if __name__ == "__main__":
    cli()
