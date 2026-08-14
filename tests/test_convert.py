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
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

# Custom libraries
from air_download.us_ct.convert import (
    ConversionError,
    build_affine,
    convert_ct_archive,
    ct,
    read_series,
    slice_positions,
    to_hounsfield,
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
