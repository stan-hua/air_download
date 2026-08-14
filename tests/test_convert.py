"""Tests for assembling a downloaded CT series into a NIfTI volume.

Every dataset here is synthesised in ``tmp_path``; nothing reads a real study.
"""

# Standard libraries
import zipfile
from pathlib import Path

# Non-standard libraries
import nibabel as nib
import numpy as np
import pytest
import zarr
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

# Custom libraries
from air_download.us_ct.convert import (
    ConversionError,
    build_affine,
    convert_ct_archive,
    convert_us_archive,
    ct,
    is_grayscale,
    read_series,
    region_box,
    slice_positions,
    to_hounsfield,
    us,
)

SERIES_UID = generate_uid()


def make_ct_slice(
    path: Path,
    value: int,
    z: float,
    instance_number: int = 1,
    orientation=(1, 0, 0, 0, 1, 0),
    pixel_spacing=(0.8, 0.9),
    shape=(4, 6),
    intercept: float = -1024.0,
    slope: float = 1.0,
    modality: str = "CT",
) -> Path:
    """Write one synthetic CT slice whose pixels are a constant."""
    rows, columns = shape
    ds = Dataset()
    ds.Modality = modality
    ds.SeriesInstanceUID = SERIES_UID
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = CTImageStorage
    ds.InstanceNumber = instance_number
    ds.ImageOrientationPatient = list(orientation)
    ds.ImagePositionPatient = [10.0, 20.0, float(z)]
    ds.PixelSpacing = list(pixel_spacing)
    ds.Rows, ds.Columns = rows, columns
    ds.RescaleIntercept, ds.RescaleSlope = intercept, slope
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = np.full((rows, columns), value, dtype=np.uint16).tobytes()

    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(path, enforce_file_format=True)
    return path


def make_ct_archive(path: Path, slices: list[dict]) -> Path:
    """Zip up one synthetic CT series, described one dict per slice."""
    staging = path.parent / f"_staging_{path.stem}"
    staging.mkdir(parents=True, exist_ok=True)
    members = [
        make_ct_slice(staging / f"CT{i:04d}.dcm", **spec)
        for i, spec in enumerate(slices)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for member in members:
            zf.write(member, arcname=member.name)
    return path


def stack(tmp_path, name="A0002.zip", **overrides) -> Path:
    """A three-slice series at z = 0, 2, 4 with distinct pixel values."""
    specs = [
        {"value": 1000, "z": 0.0, "instance_number": 1},
        {"value": 2000, "z": 2.0, "instance_number": 2},
        {"value": 3000, "z": 4.0, "instance_number": 3},
    ]
    for spec in specs:
        spec.update(overrides)
    return make_ct_archive(tmp_path / name, specs)


class TestSliceOrdering:
    """Position along the stack decides the order, not InstanceNumber."""

    def test_positions_project_onto_the_slice_normal(self, tmp_path):
        archive = stack(tmp_path)
        assert list(slice_positions(read_series(archive))) == [0.0, 2.0, 4.0]

    def test_a_lying_instance_number_does_not_shuffle_the_volume(self, tmp_path):
        # Instance numbers are assigned by the scanner and are wrong often
        # enough to silently produce a scrambled volume.
        archive = make_ct_archive(
            tmp_path / "A0002.zip",
            [
                {"value": 3000, "z": 4.0, "instance_number": 1},
                {"value": 1000, "z": 0.0, "instance_number": 2},
                {"value": 2000, "z": 2.0, "instance_number": 3},
            ],
        )
        out = convert_ct_archive(archive, tmp_path / "out.nii.gz")
        volume = np.asarray(nib.load(out).dataobj)
        # -1024 intercept, so 1000 -> -24, 2000 -> 976, 3000 -> 1976.
        assert list(volume[0, 0, :]) == [-24, 976, 1976]

    def test_archive_order_does_not_decide_the_volume(self, tmp_path):
        forward = stack(tmp_path, name="forward.zip")
        backward = make_ct_archive(
            tmp_path / "backward.zip",
            [
                {"value": 3000, "z": 4.0},
                {"value": 2000, "z": 2.0},
                {"value": 1000, "z": 0.0},
            ],
        )
        a = np.asarray(nib.load(convert_ct_archive(forward, tmp_path / "a.nii.gz")).dataobj)
        b = np.asarray(nib.load(convert_ct_archive(backward, tmp_path / "b.nii.gz")).dataobj)
        assert np.array_equal(a, b)


class TestVolume:
    """Shape, units, and dtype of the assembled array."""

    def test_hounsfield_rescale_is_applied(self, tmp_path):
        datasets = read_series(stack(tmp_path))
        datasets.sort(key=lambda d: float(d.ImagePositionPatient[2]))
        assert to_hounsfield(datasets)[0, 0, 0] == -24

    def test_a_non_unit_slope_is_applied(self, tmp_path):
        archive = stack(tmp_path, slope=2.0)
        datasets = read_series(archive)
        datasets.sort(key=lambda d: float(d.ImagePositionPatient[2]))
        assert to_hounsfield(datasets)[0, 0, 0] == 2 * 1000 - 1024

    def test_the_volume_is_int16(self, tmp_path):
        # float32 would double every volume to store integers.
        datasets = read_series(stack(tmp_path))
        assert to_hounsfield(datasets).dtype == np.int16

    def test_shape_is_columns_rows_slices(self, tmp_path):
        # Rows=4, Columns=6, 3 slices -- the array leads with columns so it
        # matches the affine, whose first column is the column direction.
        datasets = read_series(stack(tmp_path))
        assert to_hounsfield(datasets).shape == (6, 4, 3)


class TestAffine:
    """The geometry a bare array would lose."""

    def test_voxel_sizes_match_the_dicom_spacing(self, tmp_path):
        # PixelSpacing is [between rows, between columns] = [0.8, 0.9], and
        # slices sit 2 mm apart.
        datasets = read_series(stack(tmp_path))
        datasets.sort(key=lambda d: float(d.ImagePositionPatient[2]))
        affine = build_affine(datasets[0], 2.0)
        sizes = np.sqrt((affine[:3, :3] ** 2).sum(axis=0))
        assert np.allclose(sizes, [0.9, 0.8, 2.0])

    def test_the_affine_is_ras_not_lps(self, tmp_path):
        datasets = read_series(stack(tmp_path))
        affine = build_affine(datasets[0], 2.0)
        assert nib.aff2axcodes(affine) == ("L", "P", "S")

    def test_the_written_file_keeps_the_spacing(self, tmp_path):
        out = convert_ct_archive(stack(tmp_path), tmp_path / "out.nii.gz")
        assert np.allclose(nib.load(out).header.get_zooms(), (0.9, 0.8, 2.0))

    def test_anisotropic_spacing_survives(self, tmp_path):
        # The reason a plain .npy will not do: spacing varies per patient.
        archive = stack(tmp_path, pixel_spacing=(0.744140625, 0.744140625))
        out = convert_ct_archive(archive, tmp_path / "out.nii.gz")
        zooms = nib.load(out).header.get_zooms()
        assert np.isclose(zooms[0], 0.744140625)
        assert np.isclose(zooms[2], 2.0)


class TestRejectsWhatItCannotStack:
    """Better to fail than to write a volume that is quietly wrong."""

    def test_duplicate_positions_raise(self, tmp_path):
        archive = make_ct_archive(
            tmp_path / "A0002.zip",
            [{"value": 1000, "z": 0.0}, {"value": 2000, "z": 0.0}],
        )
        with pytest.raises(ConversionError, match="same position"):
            convert_ct_archive(archive, tmp_path / "out.nii.gz")

    def test_mixed_orientation_raises(self, tmp_path):
        archive = make_ct_archive(
            tmp_path / "A0002.zip",
            [
                {"value": 1000, "z": 0.0},
                {"value": 2000, "z": 2.0, "orientation": (0, 1, 0, 0, 0, -1)},
            ],
        )
        with pytest.raises(ConversionError, match="ImageOrientationPatient"):
            convert_ct_archive(archive, tmp_path / "out.nii.gz")

    def test_mixed_slice_size_raises(self, tmp_path):
        archive = make_ct_archive(
            tmp_path / "A0002.zip",
            [
                {"value": 1000, "z": 0.0},
                {"value": 2000, "z": 2.0, "shape": (8, 8)},
            ],
        )
        with pytest.raises(ConversionError, match="disagree on size"):
            convert_ct_archive(archive, tmp_path / "out.nii.gz")

    def test_an_archive_with_no_ct_raises(self, tmp_path):
        archive = make_ct_archive(
            tmp_path / "A0001.zip", [{"value": 1, "z": 0.0, "modality": "US"}]
        )
        with pytest.raises(ConversionError, match="No CT instances"):
            convert_ct_archive(archive, tmp_path / "out.nii.gz")

    def test_uneven_spacing_warns_but_still_converts(self, tmp_path, caplog):
        archive = make_ct_archive(
            tmp_path / "A0002.zip",
            [
                {"value": 1000, "z": 0.0},
                {"value": 2000, "z": 2.0},
                {"value": 3000, "z": 9.0},
            ],
        )
        with caplog.at_level("WARNING"):
            out = convert_ct_archive(archive, tmp_path / "out.nii.gz")
        assert "Slice spacing varies" in caplog.text
        assert out.exists()


class TestCohortRun:
    """Walking a cohort tree, mirroring its layout."""

    def _cohort(self, tmp_path):
        root = tmp_path / "cohort"
        stack(root / "P0001" / "visit-01" / "ct")
        return root

    def test_output_mirrors_the_pseudonymous_layout(self, tmp_path):
        root = self._cohort(tmp_path)
        ct(root, tmp_path / "nifti")
        assert (
            tmp_path / "nifti" / "P0001" / "visit-01" / "ct" / "A0002.nii.gz"
        ).exists()

    def test_the_ultrasound_half_is_left_alone(self, tmp_path):
        root = self._cohort(tmp_path)
        make_ct_archive(
            root / "P0001" / "visit-01" / "us" / "A0001.zip",
            [{"value": 1, "z": 0.0, "modality": "US"}],
        )
        ct(root, tmp_path / "nifti")
        assert not (tmp_path / "nifti" / "P0001" / "visit-01" / "us").exists()

    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        root = self._cohort(tmp_path)
        ct(root, tmp_path / "nifti", dry_run=True)
        assert not (tmp_path / "nifti").exists()
        assert "A0002.nii.gz" in capsys.readouterr().out

    def test_refuses_to_write_inside_its_input(self, tmp_path):
        root = self._cohort(tmp_path)
        with pytest.raises(ValueError, match="is inside input"):
            ct(root, root / "nifti")

    def test_one_bad_archive_does_not_stop_the_run(self, tmp_path, caplog):
        root = self._cohort(tmp_path)
        stack(root / "P0002" / "visit-01" / "ct")
        (root / "P0003" / "visit-01" / "ct").mkdir(parents=True)
        (root / "P0003" / "visit-01" / "ct" / "A0009.zip").write_bytes(b"not a zip")

        with caplog.at_level("INFO"):
            ct(root, tmp_path / "nifti")
        assert "2 NIfTI volume(s)" in caplog.text
        assert "1 failed" in caplog.text

    def test_the_run_logs_counts_not_archives(self, tmp_path, caplog):
        # The names are pseudonyms now, so this is convention rather than
        # exposure -- but a log line that lists archives does not scale to
        # thousands of them either.
        root = self._cohort(tmp_path)
        with caplog.at_level("DEBUG"):
            ct(root, tmp_path / "nifti")
        assert "A0002" not in caplog.text

    def test_missing_input_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ct(tmp_path / "nope", tmp_path / "nifti")


# --------------------------------------------------------------------------
# Ultrasound
# --------------------------------------------------------------------------

US_H, US_W = 120, 160
US_BOX = (20, 10, 140, 110)          # x0, y0, x1, y1


def make_us_clip(
    path: Path,
    n_frames: int = 30,
    region=US_BOX,
    colour: bool = False,
    moving: bool = True,
    banner: bool = True,
) -> Path:
    """Write a synthetic multi-frame ultrasound instance.

    A bright drifting blob inside the region box, plus a static bright mark
    outside it standing in for the burned-in patient banner.
    """
    frames = []
    for t in range(n_frames):
        frame = np.zeros((US_H, US_W), dtype=np.uint8)
        offset = (t * 2) % 6 if moving else 0
        value = 80 + (t * 15) % 150 if moving else 150
        frame[40 + offset:90 + offset, 50:110] = value
        if banner:
            # Outside the region box: this must never survive.
            frame[0:8, 0:60] = 255
        frames.append(frame)
    pixels = np.stack(frames)
    if colour:
        pixels = np.repeat(pixels[..., None], 3, axis=-1)
        pixels[:, 45:55, 55:65, 0] = 250      # a patch of real colour

    ds = Dataset()
    ds.Modality = "US"
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.3.1"
    ds.NumberOfFrames = n_frames
    ds.Rows, ds.Columns = US_H, US_W
    ds.SamplesPerPixel = 3 if colour else 1
    ds.PhotometricInterpretation = "RGB" if colour else "MONOCHROME2"
    if colour:
        ds.PlanarConfiguration = 0
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = pixels.tobytes()

    if region is not None:
        item = Dataset()
        item.RegionLocationMinX0, item.RegionLocationMinY0 = region[0], region[1]
        item.RegionLocationMaxX1, item.RegionLocationMaxY1 = region[2], region[3]
        ds.SequenceOfUltrasoundRegions = Sequence([item])

    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(path, enforce_file_format=True)
    return path


def make_us_archive(path: Path, clips: list[dict]) -> Path:
    """Zip up one synthetic ultrasound exam, described one dict per clip."""
    staging = path.parent / f"_staging_{path.stem}"
    staging.mkdir(parents=True, exist_ok=True)
    members = [
        make_us_clip(staging / f"US{i:04d}.dcm", **spec)
        for i, spec in enumerate(clips)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for member in members:
            zf.write(member, arcname=member.name)
    return path


class TestRegionBox:
    """The scanner's own statement of which pixels are image."""

    def test_reads_the_declared_region(self, tmp_path):
        path = make_us_clip(tmp_path / "us.dcm")
        from pydicom import dcmread as read

        assert region_box(read(path)) == US_BOX

    def test_a_missing_region_refuses_rather_than_using_the_whole_frame(
        self, tmp_path
    ):
        # Falling back to the full frame would write the banner.
        path = make_us_clip(tmp_path / "us.dcm", region=None)
        from pydicom import dcmread as read

        with pytest.raises(ConversionError, match="no SequenceOfUltrasoundRegions"):
            region_box(read(path))


class TestGrayscaleDetection:
    def test_three_identical_channels_are_grayscale(self):
        frames = np.repeat(
            np.full((4, 10, 10, 1), 120, np.uint8), 3, axis=-1
        )
        assert is_grayscale(frames)

    def test_real_colour_is_kept(self):
        frames = np.zeros((4, 10, 10, 3), np.uint8)
        frames[..., 0] = 200
        assert not is_grayscale(frames)

    def test_a_little_jpeg_chroma_noise_still_counts_as_gray(self):
        rng = np.random.default_rng(0)
        frames = np.repeat(np.full((4, 40, 40, 1), 120, np.uint8), 3, axis=-1)
        frames[..., 1] += rng.integers(0, 3, (4, 40, 40), dtype=np.uint8)
        assert is_grayscale(frames)


class TestUltrasoundConversion:
    """Region crop, beamform crop, channel collapse, one array per clip."""

    def test_the_banner_never_survives(self, tmp_path):
        # The whole point. The banner sits at rows 0-8, the region starts at
        # row 10, so nothing above it can reach the output.
        archive = make_us_archive(tmp_path / "A0001.zip", [{}])
        convert_us_archive(archive, tmp_path / "A0001.zarr", min_frames=20)
        group = zarr.open_group(tmp_path / "A0001.zarr", mode="r")
        clip = group["clip-0000"][:]
        assert clip.max() < 255
        assert clip.shape[1] <= US_BOX[3] - US_BOX[1]
        assert clip.shape[2] <= US_BOX[2] - US_BOX[0]

    def test_a_clip_with_real_colour_keeps_its_channels(self, tmp_path):
        # Colour Doppler flow is the signal; collapsing it destroys the study.
        archive = make_us_archive(tmp_path / "A0001.zip", [{"colour": True}])
        convert_us_archive(archive, tmp_path / "A0001.zarr", min_frames=20)
        clip = zarr.open_group(tmp_path / "A0001.zarr", mode="r")["clip-0000"]
        assert clip.attrs["grayscale"] is False
        assert clip.ndim == 4 and clip.shape[-1] == 3

    def test_a_truly_grayscale_clip_loses_its_channel_axis(self, tmp_path):
        archive = make_us_archive(tmp_path / "A0001.zip", [{}])
        convert_us_archive(archive, tmp_path / "A0001.zarr", min_frames=20)
        clip = zarr.open_group(tmp_path / "A0001.zarr", mode="r")["clip-0000"]
        assert clip.ndim == 3
        assert clip.attrs["grayscale"] is True

    def test_short_clips_are_skipped(self, tmp_path):
        archive = make_us_archive(
            tmp_path / "A0001.zip", [{"n_frames": 30}, {"n_frames": 3}]
        )
        written = convert_us_archive(archive, tmp_path / "A0001.zarr", min_frames=20)
        assert written == 1

    def test_clip_names_carry_the_member_index(self, tmp_path):
        # So the arrays join straight to frames.csv.
        archive = make_us_archive(
            tmp_path / "A0001.zip", [{"n_frames": 3}, {"n_frames": 30}]
        )
        convert_us_archive(archive, tmp_path / "A0001.zarr", min_frames=20)
        group = zarr.open_group(tmp_path / "A0001.zarr", mode="r")
        assert list(group.array_keys()) == ["clip-0001"]
        assert group["clip-0001"].attrs["member_index"] == 1

    def test_frames_are_chunked_for_compression(self, tmp_path):
        # Several frames per chunk is what lets zstd find the redundancy
        # between them; one frame per chunk measured 38% of raw against 22%.
        archive = make_us_archive(tmp_path / "A0001.zip", [{}])
        convert_us_archive(archive, tmp_path / "A0001.zarr", min_frames=20)
        clip = zarr.open_group(tmp_path / "A0001.zarr", mode="r")["clip-0000"]
        assert clip.chunks[0] == 8
        assert clip.chunks[1:] == clip.shape[1:]

    def test_a_clip_shorter_than_the_chunk_is_not_padded(self, tmp_path):
        archive = make_us_archive(tmp_path / "A0001.zip", [{"n_frames": 5}])
        convert_us_archive(
            archive, tmp_path / "A0001.zarr", min_frames=2, chunk_frames=8
        )
        clip = zarr.open_group(tmp_path / "A0001.zarr", mode="r")["clip-0000"]
        assert clip.chunks[0] == 5

    def test_frame_count_is_preserved(self, tmp_path):
        archive = make_us_archive(tmp_path / "A0001.zip", [{"n_frames": 37}])
        convert_us_archive(archive, tmp_path / "A0001.zarr", min_frames=20)
        clip = zarr.open_group(tmp_path / "A0001.zarr", mode="r")["clip-0000"]
        assert clip.shape[0] == 37

    def test_tightening_crops_further_than_the_region(self, tmp_path):
        archive = make_us_archive(tmp_path / "A0001.zip", [{}])
        convert_us_archive(archive, tmp_path / "loose.zarr", min_frames=20, tighten=False)
        convert_us_archive(archive, tmp_path / "tight.zarr", min_frames=20, tighten=True)
        loose = zarr.open_group(tmp_path / "loose.zarr", mode="r")["clip-0000"]
        tight = zarr.open_group(tmp_path / "tight.zarr", mode="r")["clip-0000"]
        assert tight.shape[1] <= loose.shape[1]
        assert tight.shape[2] < loose.shape[2]
        assert tight.attrs["beamform_box"] is not None

    def test_a_frozen_clip_falls_back_to_the_region_crop(self, tmp_path):
        # Nothing moves, so there is no beamform to find -- but the region
        # crop already made it safe, so the clip is kept rather than dropped.
        archive = make_us_archive(tmp_path / "A0001.zip", [{"moving": False}])
        convert_us_archive(archive, tmp_path / "A0001.zarr", min_frames=20)
        clip = zarr.open_group(tmp_path / "A0001.zarr", mode="r")["clip-0000"]
        assert clip.attrs["beamform_box"] is None
        assert clip.shape[1:] == (US_BOX[3] - US_BOX[1], US_BOX[2] - US_BOX[0])

    def test_an_archive_with_no_long_clip_raises(self, tmp_path):
        archive = make_us_archive(tmp_path / "A0001.zip", [{"n_frames": 2}])
        with pytest.raises(ConversionError, match="No ultrasound clip"):
            convert_us_archive(archive, tmp_path / "A0001.zarr", min_frames=20)


class TestUltrasoundCohortRun:
    def _cohort(self, tmp_path):
        root = tmp_path / "cohort"
        make_us_archive(root / "P0001" / "visit-01" / "us" / "A0001.zip", [{}])
        return root

    def test_output_mirrors_the_pseudonymous_layout(self, tmp_path):
        root = self._cohort(tmp_path)
        us(root, tmp_path / "arrays", min_frames=20)
        assert (
            tmp_path / "arrays" / "P0001" / "visit-01" / "us" / "A0001.zarr"
        ).is_dir()

    def test_the_ct_half_is_left_alone(self, tmp_path):
        root = self._cohort(tmp_path)
        stack(root / "P0001" / "visit-01" / "ct")
        us(root, tmp_path / "arrays", min_frames=20)
        assert not (tmp_path / "arrays" / "P0001" / "visit-01" / "ct").exists()

    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        root = self._cohort(tmp_path)
        us(root, tmp_path / "arrays", dry_run=True)
        assert not (tmp_path / "arrays").exists()
        assert "A0001.zarr" in capsys.readouterr().out

    def test_refuses_to_write_inside_its_input(self, tmp_path):
        root = self._cohort(tmp_path)
        with pytest.raises(ValueError, match="is inside input"):
            us(root, root / "arrays")

    def test_one_bad_archive_does_not_stop_the_run(self, tmp_path, caplog):
        root = self._cohort(tmp_path)
        make_us_archive(root / "P0002" / "visit-01" / "us" / "A0003.zip", [{}])
        bad = root / "P0003" / "visit-01" / "us"
        bad.mkdir(parents=True)
        (bad / "A0009.zip").write_bytes(b"not a zip")

        with caplog.at_level("INFO"):
            us(root, tmp_path / "arrays", min_frames=20)
        assert "2 clip(s) from 2 archive(s)" in caplog.text
        assert "1 archive(s) failed" in caplog.text

    def test_a_zero_threshold_raises(self, tmp_path):
        root = self._cohort(tmp_path)
        with pytest.raises(ValueError, match="min_frames must be at least 1"):
            us(root, tmp_path / "arrays", min_frames=0)
