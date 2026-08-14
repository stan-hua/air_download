"""
crosswalk.py

Description: Map real patient and exam identifiers onto sequential pseudonyms,
             and keep the mapping in one file you can store apart from the data.

A downloaded cohort is filed under `P0001/visit-01/us/A0001.zip`, so nothing in
a path, a progress bar, a traceback, or a derived CSV is an identifier. This
module holds the only link back. Guard it accordingly: it is a more dangerous
file than any it replaces, and losing it orphans the cohort permanently.

Three maps, all assign-on-first-seen and all persisted, are what make a run
resumable without duplicating anyone:

===================  ======================================  ===========
Map                  Key                                     Value
===================  ======================================  ===========
patient              ``mrn``                                 ``P0001``
exam                 ``(mrn, accession)``                    ``A0001``
visit                ``(mrn, us_accession, ct_accession)``   ``visit-01``
===================  ======================================  ===========

An exam is keyed on the **pair**, never the accession alone: the same accession
number belongs to different patients, so keying on it would file two patients'
exams as one. Counters resume from the highest identifier already in the file
rather than from its row count, so a truncated or hand-edited crosswalk cannot
reissue an identifier that a folder on disk is already using.

The first five columns carry no PHI, so ``cut -d, -f1-5 crosswalk.csv`` is a
shareable description of the cohort's whole structure.

Examples
--------
Assign identifiers and record where an archive landed::

    from air_download.crosswalk import Crosswalk

    crosswalk = Crosswalk("cohort_crosswalk.csv")
    anon_mrn = crosswalk.patient_id(mrn)
    anon_accession = crosswalk.exam_id(mrn, accession)
    visit = crosswalk.visit_folder(mrn, us_accession, ct_accession, us_date_time)
    crosswalk.record(
        mrn=mrn,
        accession=accession,
        exam_type="us",
        date_time=us_date_time,
        visit_folder=visit,
        archive_path=f"{anon_mrn}/{visit}/us/{anon_accession}.zip",
    )

Recover the identifiers a downstream CSV refers to::

    from air_download.crosswalk import parse_anon_ids

    parse_anon_ids("P0001/visit-01/us/A0001.zip")   # ("P0001", "A0001")
"""

# Standard libraries
import csv
import logging
import re
from datetime import datetime
from pathlib import Path

# Custom libraries
from air_download.utils import as_identifier, parse_datetime

logger = logging.getLogger(__name__)

PATIENT_ID_PREFIX = "P"
EXAM_ID_PREFIX = "A"
ID_DIGITS = 4

VISIT_PREFIX = "visit-"
VISIT_DIGITS = 2

CROSSWALK_CSV_HEADER = [
    # Nothing above this line identifies anyone, which is what makes
    # `cut -d, -f1-5` a safe projection of the cohort.
    "anon_mrn",
    "anon_accession_number",
    "visit_folder",
    "exam_type",
    "archive_path",
    # PHI from here down.
    "mrn",
    "accession_number",
    "date_time",
]

_PATIENT_ID_PATTERN = re.compile(rf"^{PATIENT_ID_PREFIX}(\d+)$")
_EXAM_ID_PATTERN = re.compile(rf"^{EXAM_ID_PREFIX}(\d+)$")
_VISIT_PATTERN = re.compile(rf"^{VISIT_PREFIX}(\d+)$")


def default_crosswalk_path(output: str | Path) -> Path:
    """Name the crosswalk for a cohort, as a sibling of its output directory.

    A sibling rather than a child, so that archiving or sharing the cohort
    cannot carry the key along with the lock.

    Parameters
    ----------
    output : str or Path
        Root directory of the cohort.

    Returns
    -------
    Path
        ``<output>_crosswalk.csv`` next to ``output``.
    """
    output = Path(output)
    if not output.name:
        # `.` or `/` has no name to build on.
        return Path("cohort_crosswalk.csv")
    return output.parent / f"{output.name}_crosswalk.csv"


def format_patient_id(index: int) -> str:
    """Render a patient's ordinal as ``P0001``."""
    return f"{PATIENT_ID_PREFIX}{index:0{ID_DIGITS}d}"


def format_exam_id(index: int) -> str:
    """Render an exam's ordinal as ``A0001``."""
    return f"{EXAM_ID_PREFIX}{index:0{ID_DIGITS}d}"


def format_visit_folder(index: int) -> str:
    """Render a visit's ordinal within its patient as ``visit-01``."""
    return f"{VISIT_PREFIX}{index:0{VISIT_DIGITS}d}"


def _ordinal(value: str, pattern: re.Pattern) -> int:
    """Return the number inside a generated identifier, or 0 if it is not one."""
    match = pattern.match(value.strip())
    # int() on a counter, not on an identifier: the digits of "P0007" are an
    # ordinal this module generated, never anything a hospital issued.
    return int(match.group(1)) if match else 0


def parse_anon_ids(archive: str | Path) -> tuple[str, str]:
    """Recover the anonymous identifiers a cohort path was built from.

    This is the reader for what :class:`Crosswalk` writes, and it lives beside
    the writer so the two cannot drift.

    Both values are empty unless the path carries a ``P<digits>`` component.
    That guard matters: some sites issue real accession numbers shaped like
    ``A0001``, and without it a path outside the cohort layout would put a real
    identifier into a column named ``anon_accession_number``.

    Parameters
    ----------
    archive : str or Path
        An archive path relative to the cohort root, or a directory within it.

    Returns
    -------
    tuple of str
        ``(anon_mrn, anon_accession_number)``, either of which may be empty.

    Examples
    --------
    >>> parse_anon_ids("P0001/visit-01/us/A0001.zip")
    ('P0001', 'A0001')
    >>> parse_anon_ids("P0001/visit-01/us")
    ('P0001', '')
    >>> parse_anon_ids("A0001.zip")
    ('', '')
    """
    parts = Path(archive).parts

    anon_mrn = next((p for p in parts if _PATIENT_ID_PATTERN.match(p)), "")
    if not anon_mrn:
        return "", ""

    anon_accession = ""
    for part in reversed(parts):
        candidate = part[:-4] if part.lower().endswith(".zip") else part
        if _EXAM_ID_PATTERN.match(candidate):
            anon_accession = candidate
            break
    return anon_mrn, anon_accession


def is_anon_mrn(name: str) -> bool:
    """Report whether a path component is a generated patient identifier."""
    return bool(_PATIENT_ID_PATTERN.match(name))


def _zero_stripped(value: str) -> str:
    """Collapse an identifier to the form Excel would have left it in."""
    return value.lstrip("0") or "0"


class Crosswalk:
    """The mapping between real identifiers and the pseudonyms on disk.

    Rows are appended the moment they are recorded, so a run that dies partway
    still has a usable mapping for everything that reached disk. Loading an
    existing file is what makes a resumed run reuse identifiers instead of
    minting duplicates.

    Parameters
    ----------
    path : str or Path
        The crosswalk CSV. Read if it exists, created on the first record.
    read_only : bool, optional
        Assign identifiers in memory and write nothing. Used by ``--dry_run``.

    Raises
    ------
    ValueError
        If an existing file is missing a required column.
    """

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only

        self._patients: dict[str, str] = {}
        self._exams: dict[tuple[str, str], str] = {}
        self._visits: dict[tuple[str, str, str], str] = {}
        self._recorded: set[str] = set()

        self._patient_count = 0
        self._exam_count = 0
        self._visit_counts: dict[str, int] = {}
        self._visit_times: dict[str, list[datetime]] = {}

        # Zero-stripped forms, to catch an MRN that lost a leading zero
        # somewhere upstream and would otherwise be filed as a new patient.
        self._stripped_mrns: dict[str, str] = {}
        self._stripped_accessions: dict[tuple[str, str], str] = {}

        self._load()

    # -- loading -----------------------------------------------------------

    def _load(self) -> None:
        """Rebuild every map and counter from an existing crosswalk."""
        if not self.path.exists():
            return

        with open(self.path, newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            missing = [c for c in CROSSWALK_CSV_HEADER if c not in fieldnames]
            if missing:
                raise ValueError(
                    f"{self.path} is missing required column(s): "
                    f"{', '.join(missing)}. Found: "
                    f"{', '.join(fieldnames) if fieldnames else '(no header row)'}."
                )
            rows = [dict(row) for row in reader]

        # Pairs are reconstructed per (patient, visit folder), because a row
        # describes one exam while a visit is keyed on both of its accessions.
        pairs: dict[tuple[str, str], dict[str, str]] = {}

        for row in rows:
            mrn = as_identifier(row.get("mrn"))
            accession = as_identifier(row.get("accession_number"))
            anon_mrn = (row.get("anon_mrn") or "").strip()
            anon_accession = (row.get("anon_accession_number") or "").strip()
            visit = (row.get("visit_folder") or "").strip()
            archive = (row.get("archive_path") or "").strip()

            if mrn and anon_mrn:
                self._patients.setdefault(mrn, anon_mrn)
                self._stripped_mrns.setdefault(_zero_stripped(mrn), mrn)
                self._patient_count = max(
                    self._patient_count, _ordinal(anon_mrn, _PATIENT_ID_PATTERN)
                )
            if mrn and accession and anon_accession:
                self._exams.setdefault((mrn, accession), anon_accession)
                self._stripped_accessions.setdefault(
                    (mrn, _zero_stripped(accession)), accession
                )
                self._exam_count = max(
                    self._exam_count, _ordinal(anon_accession, _EXAM_ID_PATTERN)
                )
            if archive:
                self._recorded.add(archive)
            if mrn and visit:
                self._visit_counts[mrn] = max(
                    self._visit_counts.get(mrn, 0), _ordinal(visit, _VISIT_PATTERN)
                )
                slot = pairs.setdefault((mrn, visit), {})
                exam_type = (row.get("exam_type") or "").strip().lower()
                if exam_type in ("us", "ct"):
                    slot[exam_type] = accession
                if exam_type == "us":
                    self._remember_visit_time(mrn, row.get("date_time") or "")

        for (mrn, visit), slot in pairs.items():
            us, ct = slot.get("us", ""), slot.get("ct", "")
            if us and ct:
                self._visits[(mrn, us, ct)] = visit

        logger.info(
            "Loaded a crosswalk of %d patient(s) and %d archive(s) from %s.",
            len(self._patients),
            len(self._recorded),
            self.path,
        )

    def _remember_visit_time(self, mrn: str, date_time: str) -> None:
        """Note when a patient's already-numbered visit happened."""
        if not date_time:
            return
        try:
            parsed = parse_datetime(date_time)
        except ValueError:
            return
        self._visit_times.setdefault(mrn, []).append(parsed)

    # -- assignment --------------------------------------------------------

    def patient_id(self, mrn: str) -> str:
        """Return this patient's anonymous MRN, assigning one on first sight.

        Parameters
        ----------
        mrn : str
            The real MRN, as text.

        Returns
        -------
        str
            ``P0001``-style identifier, stable for the life of the crosswalk.
        """
        mrn = as_identifier(mrn)
        existing = self._patients.get(mrn)
        if existing is not None:
            return existing

        stripped = _zero_stripped(mrn)
        if stripped in self._stripped_mrns:
            # Two such MRNs really are two patients, and are filed as two.
            # But far more often one of them lost a zero on the way in, so
            # say so rather than letting a cohort quietly gain a patient.
            logger.warning(
                "Two MRNs in this cohort differ only by leading zeros. They "
                "are being filed as two patients, which is correct if both "
                "are real -- but Excel and pandas.read_csv without dtype=str "
                "both strip leading zeros, so check the source CSV. The "
                "values are not named here, because they are identifiers."
            )

        self._patient_count += 1
        anon = format_patient_id(self._patient_count)
        self._patients[mrn] = anon
        self._stripped_mrns[stripped] = mrn
        return anon

    def exam_id(self, mrn: str, accession: str) -> str:
        """Return this exam's anonymous accession, assigning one on first sight.

        Keyed on the ``(mrn, accession)`` pair, never the accession alone: the
        same accession number can belong to more than one patient.

        Parameters
        ----------
        mrn : str
            The real MRN, as text.
        accession : str
            The real accession number, as text.

        Returns
        -------
        str
            ``A0001``-style identifier, unique across the whole cohort.
        """
        mrn = as_identifier(mrn)
        accession = as_identifier(accession)
        existing = self._exams.get((mrn, accession))
        if existing is not None:
            return existing

        stripped = (mrn, _zero_stripped(accession))
        if stripped in self._stripped_accessions:
            # As for MRNs: legal, but far more often a lost leading zero.
            logger.warning(
                "Two accession numbers for one patient differ only by leading "
                "zeros. They are being filed as two exams; check the source "
                "CSV for a lost zero. The values are not named here, because "
                "they are identifiers."
            )

        self._exam_count += 1
        anon = format_exam_id(self._exam_count)
        self._exams[(mrn, accession)] = anon
        self._stripped_accessions[stripped] = accession
        return anon

    def visit_folder(
        self,
        mrn: str,
        us_accession: str,
        ct_accession: str,
        us_date_time: str = "",
    ) -> str:
        """Return this visit's folder name, assigning an ordinal on first sight.

        Ordinals count within a patient and are never renumbered, because
        renumbering would move folders that are already on disk. Sort a run's
        rows by ``us_date_time`` before calling this and the ordinals come out
        chronological; a visit first seen in a later run is appended and warned
        about if it turns out to predate one already numbered.

        Parameters
        ----------
        mrn : str
            The real MRN, as text.
        us_accession : str
            The ultrasound's real accession number.
        ct_accession : str
            The CT's real accession number.
        us_date_time : str, optional
            The ultrasound's timestamp, used only to check the ordering.

        Returns
        -------
        str
            ``visit-01``-style folder name, unique within the patient.
        """
        mrn = as_identifier(mrn)
        key = (mrn, as_identifier(us_accession), as_identifier(ct_accession))
        existing = self._visits.get(key)
        if existing is not None:
            return existing

        when = None
        if us_date_time:
            try:
                when = parse_datetime(us_date_time)
            except ValueError:
                when = None
        if when is not None and any(p > when for p in self._visit_times.get(mrn, [])):
            logger.warning(
                "A visit being added is earlier than one already numbered for "
                "the same patient, so its ordinal does not reflect the visit "
                "order. Renumbering would move folders already on disk; sort "
                "by the crosswalk's date_time column instead."
            )

        count = self._visit_counts.get(mrn, 0) + 1
        self._visit_counts[mrn] = count
        folder = format_visit_folder(count)
        self._visits[key] = folder
        if when is not None:
            self._visit_times.setdefault(mrn, []).append(when)
        return folder

    # -- recording ---------------------------------------------------------

    def record(
        self,
        *,
        mrn: str,
        accession: str,
        exam_type: str,
        date_time: str,
        visit_folder: str,
        archive_path: str | Path,
    ) -> None:
        """Write one row linking an archive back to its real identifiers.

        Call this *before* downloading, not after. The failure window then
        leaves a row for an exam that failed, which costs nothing, rather than
        an archive on disk with no way back, which cannot be repaired.

        Recording an ``archive_path`` already present is a no-op, so a resumed
        run appends nothing for what it skips.

        Parameters
        ----------
        mrn : str
            The real MRN, as text.
        accession : str
            The real accession number, as text.
        exam_type : str
            ``"us"`` or ``"ct"``.
        date_time : str
            The exam's real timestamp, verbatim from the source CSV.
        visit_folder : str
            The folder name from :meth:`visit_folder`.
        archive_path : str or Path
            The archive's path relative to the cohort root.
        """
        mrn = as_identifier(mrn)
        accession = as_identifier(accession)
        archive_path = Path(archive_path).as_posix()
        if archive_path in self._recorded:
            return
        self._recorded.add(archive_path)

        if self.read_only:
            return

        row = {
            "anon_mrn": self.patient_id(mrn),
            "anon_accession_number": self.exam_id(mrn, accession),
            "visit_folder": visit_folder,
            "exam_type": exam_type,
            "archive_path": archive_path,
            "mrn": mrn,
            "accession_number": accession,
            "date_time": date_time,
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.exists()
        with open(self.path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CROSSWALK_CSV_HEADER)
            if is_new:
                writer.writeheader()
            writer.writerow(row)

    # -- reporting ---------------------------------------------------------

    @property
    def n_patients(self) -> int:
        """Number of distinct patients the crosswalk knows about."""
        return len(self._patients)

    def __len__(self) -> int:
        """Number of archives recorded."""
        return len(self._recorded)
