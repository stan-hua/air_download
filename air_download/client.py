"""AIR API client for authentication, searching, and downloading DICOM data."""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from dotenv import dotenv_values
from tqdm import tqdm

from air_download.filters import (
    DEFAULT_AXIAL_PATTERNS,
    apply_exclusion_filter,
    apply_inclusion_filter,
    select_sr_and_thinnest_axial,
)
from air_download.utils import (
    DEFAULT_CHUNK_DAYS,
    build_date_ranges,
    build_exam_output_path,
    exam_key,
    write_exams_csv,
)

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_ID = 1

# Retry policy for transient API failures. The portal can rate-limit or
# briefly refuse a burst of queries, which chunked date searches make more
# likely, so requests are retried with exponential backoff.
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_FACTOR = 1.0
DEFAULT_MAX_BACKOFF = 60.0
RETRY_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Modality codes accepted by the `modality` query parameter of
# /secure/search/query-data-source, per docs/air_open_api.yaml.
MODALITIES = frozenset(
    {
        "AR", "ASMT", "AU", "BDUS", "BI", "BMD", "CR", "CT", "DG", "DOC",
        "DX", "ECG", "EPS", "ES", "FID", "GM", "HC", "HD", "IO", "IOL",
        "IVOCT", "IVUS", "KER", "KO", "LEN", "LS", "MG", "MR", "NM", "OAM",
        "OCT", "OP", "OPM", "OPT", "OPV", "OSS", "OT", "PLAN", "PR", "PT",
        "PX", "RF", "REG", "RESP", "RTDOSE", "RTIMAGE", "RTPLAN", "RTRECORD",
        "RTSTRUCT", "RMV", "SEG", "SM", "SR", "SRF", "STAIN", "TG", "US",
        "VA", "XA", "XC",
    }
)


def normalize_modality(modality: str | None) -> str:
    """Validate and upper-case a modality code for the search query.

    Args:
        modality: Modality code such as ``CT`` or ``us``. May be None or
            empty, in which case the query is not restricted by modality.

    Returns:
        The upper-cased modality code, or an empty string if none was given.

    Raises:
        ValueError: If the code is not one of the modalities the API accepts.
    """
    if not modality:
        return ""
    normalized = modality.strip().upper()
    if normalized not in MODALITIES:
        raise ValueError(
            f"Unknown modality '{modality}'. The API accepts: "
            f"{', '.join(sorted(MODALITIES))}."
        )
    return normalized


def backoff_delay(
    attempt: int,
    retry_after: str | None = None,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    max_backoff: float = DEFAULT_MAX_BACKOFF,
) -> float:
    """Compute how long to wait before retrying a request.

    A ``Retry-After`` header wins when the server sends one in seconds,
    since the server knows its own rate limit better than we do. Otherwise
    the delay doubles per attempt, capped at ``max_backoff``.

    Args:
        attempt: Zero-based index of the attempt that just failed.
        retry_after: Value of the response's ``Retry-After`` header, if any.
            Only the delay-in-seconds form is honoured; the HTTP-date form
            falls back to exponential backoff.
        backoff_factor: Delay in seconds before the first retry.
        max_backoff: Upper bound on any single delay.

    Returns:
        Seconds to sleep before the next attempt.
    """
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), max_backoff)
        except ValueError:
            logger.debug("Ignoring non-numeric Retry-After: %r", retry_after)
    return min(backoff_factor * (2**attempt), max_backoff)


class AIRClient:
    """Client for interacting with the AIR (Automated Image Retrieval) API.

    Handles authentication, exam search, and DICOM download. Credentials and
    URL can be provided directly, loaded from a dotenv-style credential file,
    or read from environment variables.

    Resolution order for URL:
        1. ``url`` argument passed to the constructor
        2. ``AIR_URL`` in the credential file
        3. ``AIR_URL`` environment variable

    Resolution order for credentials:
        1. Credential file (``AIR_USERNAME`` / ``AIR_PASSWORD``)
        2. Environment variables (``AIR_USERNAME`` / ``AIR_PASSWORD``)

    Resolution order for the project and anonymization profile:
        1. ``project`` / ``profile`` argument passed to :meth:`download`
        2. ``AIR_PROJECT`` / ``AIR_PROFILE`` in the credential file
        3. ``AIR_PROJECT`` / ``AIR_PROFILE`` environment variable

    Transient failures (connection errors, timeouts, and the status codes in
    ``RETRY_STATUS_CODES``) are retried with exponential backoff, honouring a
    numeric ``Retry-After`` header when the server sends one.

    Args:
        url: AIR API base URL. If not provided, resolved from credential
            file or ``AIR_URL`` environment variable.
        cred_path: Path to a dotenv-style credential file containing
            ``AIR_USERNAME``, ``AIR_PASSWORD``, and optionally ``AIR_URL``,
            ``AIR_PROJECT`` and ``AIR_PROFILE``. If None, values are read
            from environment variables.
        max_retries: Number of retries after the initial attempt. Zero
            disables retrying.
        backoff_factor: Delay in seconds before the first retry; each
            subsequent delay doubles.
        max_backoff: Upper bound on any single retry delay, in seconds.
    """

    def __init__(
        self,
        url: str | None = None,
        cred_path: str | Path | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
    ) -> None:
        self._cred_path = Path(cred_path) if cred_path else None
        self._envs = self._load_credential_file()
        self.url = self._resolve_url(url)
        self._session = requests.Session()
        self._jwt: str | None = None
        self._projects: list[dict[str, Any]] | None = None
        self._max_retries = max(0, max_retries)
        self._backoff_factor = backoff_factor
        self._max_backoff = max_backoff

    def _load_credential_file(self) -> dict[str, str]:
        """Load key-value pairs from the credential file if it exists.

        Returns:
            Dictionary of values from the credential file, or empty dict.

        Raises:
            FileNotFoundError: If a credential path was specified but the
                file does not exist.
        """
        if self._cred_path is None:
            return {}
        if not self._cred_path.exists():
            raise FileNotFoundError(
                f"AIR credential file ({self._cred_path}) does not exist."
            )
        return dict(dotenv_values(self._cred_path))

    def _resolve_url(self, url_arg: str | None) -> str:
        """Resolve the API URL from argument, credential file, or environment.

        Args:
            url_arg: Explicit URL argument (highest priority).

        Returns:
            The resolved API URL.

        Raises:
            ValueError: If URL cannot be resolved from any source.
        """
        url = url_arg or self._envs.get("AIR_URL") or os.environ.get("AIR_URL")
        if url:
            # Ensure trailing slash so urljoin appends paths correctly
            # (e.g. urljoin("https://host/api/", "login") → ".../api/login"
            #  vs   urljoin("https://host/api",  "login") → ".../login")
            return url if url.endswith("/") else url + "/"
        raise ValueError(
            "AIR API URL not provided. Set it via one of:\n"
            "  1. --url CLI flag\n"
            "  2. AIR_URL in the credential file\n"
            "  3. AIR_URL environment variable"
        )

    def _resolve_id(
        self,
        value: int | str | None,
        env_key: str,
        label: str,
        list_flag: str,
    ) -> int:
        """Resolve a numeric setting from argument, credential file, or environment.

        Args:
            value: Explicit value passed by the caller (highest priority).
                None means "use the configured default".
            env_key: Key to look up in the credential file and environment.
            label: Human-readable name of the setting, for error messages.
            list_flag: CLI flag that lists valid IDs, for error messages.

        Returns:
            The resolved ID, or -1 if none is configured.

        Raises:
            ValueError: If the resolved value is not an integer.
        """
        source = "argument"
        if value is None:
            value = self._envs.get(env_key)
            source = f"{env_key} in {self._cred_path}"
        if value is None:
            value = os.environ.get(env_key)
            source = f"{env_key} environment variable"
        if value is None or value == "":
            return -1

        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"{label} must be an integer, got {value!r} from {source}. "
                f"Run with {list_flag} to list valid IDs."
            ) from None

    def _resolve_profile(self, profile_arg: int | str | None) -> int:
        """Resolve the anonymization profile from argument, file, or environment.

        Args:
            profile_arg: Explicit profile passed by the caller, or None.

        Returns:
            The resolved profile ID, or -1 if none is configured.
        """
        return self._resolve_id(
            profile_arg, "AIR_PROFILE", "Anonymization profile", "-lpf"
        )

    def _resolve_project(self, project_arg: int | str | None) -> int:
        """Resolve the project ID from argument, file, or environment.

        Args:
            project_arg: Explicit project passed by the caller, or None.

        Returns:
            The resolved project ID, or -1 if none is configured.
        """
        return self._resolve_id(project_arg, "AIR_PROJECT", "Project ID", "-lpj")

    def _get_credentials(self) -> tuple[str, str]:
        """Resolve username and password from credential file or environment.

        Returns:
            Tuple of (username, password).

        Raises:
            ValueError: If credentials cannot be resolved.
        """
        username = self._envs.get("AIR_USERNAME") or os.environ.get("AIR_USERNAME")
        password = self._envs.get("AIR_PASSWORD") or os.environ.get("AIR_PASSWORD")
        if not username or not password:
            raise ValueError(
                "AIR credentials not provided. Set AIR_USERNAME and AIR_PASSWORD "
                "in the credential file or as environment variables."
            )
        return username, password

    def _post(
        self,
        endpoint: str,
        raise_for_status: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        """Make a POST request to the API, retrying transient failures.

        Connection errors, timeouts, and the status codes in
        ``RETRY_STATUS_CODES`` (rate limiting and server errors) are retried
        with exponential backoff. Every other response — including ordinary
        4xx — is returned or raised immediately, since retrying will not
        change the outcome.

        Args:
            endpoint: API endpoint path (appended to the base URL).
            raise_for_status: If True (default), raise an HTTPError for
                non-2xx responses. Set to False for endpoints that return
                non-2xx status codes with useful JSON error bodies.
            **kwargs: Additional keyword arguments passed to ``requests.post``.

        Returns:
            The response object.

        Raises:
            requests.HTTPError: If ``raise_for_status`` is True and the final
                response status code indicates an error.
            requests.RequestException: If every attempt failed to connect.
        """
        url = urljoin(self.url, endpoint)

        for attempt in range(self._max_retries + 1):
            retry_after = None
            try:
                response = self._session.post(url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == self._max_retries:
                    raise
                reason = f"{type(exc).__name__}"
            else:
                is_last = attempt == self._max_retries
                if response.status_code not in RETRY_STATUS_CODES or is_last:
                    if raise_for_status:
                        response.raise_for_status()
                    return response
                reason = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After")

            delay = backoff_delay(
                attempt, retry_after, self._backoff_factor, self._max_backoff
            )
            logger.warning(
                "%s from %s (attempt %d of %d); retrying in %.1fs.",
                reason,
                endpoint,
                attempt + 1,
                self._max_retries + 1,
                delay,
            )
            time.sleep(delay)

        # Unreachable: the loop either returns or raises on its last pass.
        raise RuntimeError(f"Request to {endpoint} exhausted retries.")

    def authenticate(self) -> None:
        """Authenticate with the AIR API and store the JWT token.

        Raises:
            ValueError: If credentials are missing or invalid.
            requests.HTTPError: If the authentication request fails.
        """
        username, password = self._get_credentials()
        auth_info = {"userId": username, "password": password}
        response = self._post("login", json=auth_info)
        session = response.json()
        
        # Check for authentication errors in the response
        if "token" not in session or "user" not in session:
            logger.error("Login response: %s", session)
            raise ValueError(
                f"Authentication failed. Server response does not contain expected "
                f"'token' and 'user' fields. Response: {session}"
            )
        
        self._jwt = session["token"]["jwt"]
        self._projects = session["user"]["projects"]
        logger.info("Authentication successful.")

    @property
    def _auth_header(self) -> dict[str, str]:
        """Return the authorization header, authenticating if needed."""
        if self._jwt is None:
            self.authenticate()
        return {"Authorization": f"Bearer {self._jwt}"}

    def list_projects(self) -> list[dict[str, Any]]:
        """List available projects from the API.

        Returns:
            List of project dictionaries with ``id`` and ``name`` keys.
        """
        if self._projects is None:
            self.authenticate()
        return self._projects

    def list_profiles(self) -> list[dict[str, Any]]:
        """List available de-identification profiles from the API.

        Returns:
            List of profile dictionaries with ``id``, ``name``, and
            ``description`` keys.
        """
        response = self._post(
            "secure/anonymization/list-profiles",
            headers=self._auth_header,
            json={
                "includeGlobal": True,
                "includeCustom": True,
                "includeDefault": False,
                "includeInactiveCustom": False,
                "includeInactiveGlobal": False,
                "includeInactiveShared": False,
                "includeShared": True,
            },
        ).json()
        return [
            {k: profile[k] for k in ("id", "name", "description")}
            for profile in response
        ]

    def search(
        self,
        accession: str | None = None,
        mrn: str | None = None,
        modality: str | None = None,
        study_description: str | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        chunk_days: int = DEFAULT_CHUNK_DAYS,
        exam_modality_inclusion: str | None = None,
        exam_description_inclusion: str | None = None,
        exam_modality_exclusion: str | None = None,
        exam_description_exclusion: str | None = None,
        source_id: int = DEFAULT_SOURCE_ID,
    ) -> list[dict[str, Any]]:
        """Search for exams by accession, MRN, modality, or study description.

        At least one of ``accession``, ``mrn``, ``modality``, or
        ``study_description`` must be given. ``modality`` and
        ``study_description`` are sent to the server as query parameters, so
        they narrow the search across all patients; the ``*_inclusion`` and
        ``*_exclusion`` arguments are applied client-side to whatever the
        server returns.

        The data source caps how many exams one query may return, so a date
        window longer than ``chunk_days`` is issued as several consecutive
        queries and the results are merged and de-duplicated.

        Args:
            accession: Accession number to search for.
            mrn: Patient MRN to search for.
            modality: Modality code to query the server for (e.g. ``CT``,
                ``US``). Validated against the codes the API accepts.
            study_description: Study description to query the server for
                (e.g. ``CT ABDOMEN PELVIS W CONTRAST``). Matching semantics
                are decided by the data source; use
                ``exam_description_inclusion`` for guaranteed substring
                matching.
            date_start: Start of the date window, ISO 8601 (e.g.
                ``2024-01-15``). Without it the search is not bounded below.
            date_end: End of the date window, ISO 8601. Defaults to the
                current time when ``date_start`` is given.
            chunk_days: Maximum span of a single query, in days.
            exam_modality_inclusion: Comma-separated modality filter patterns.
            exam_description_inclusion: Comma-separated description filter
                patterns.
            exam_modality_exclusion: Comma-separated modality exclusion patterns.
            exam_description_exclusion: Comma-separated description exclusion
                patterns.
            source_id: Data source ID for the query.

        Returns:
            List of matching exam dictionaries.

        Raises:
            ValueError: If no search criterion is provided, if ``modality``
                is not a code the API accepts, or if the date window is
                unparseable or ends before it starts.
        """
        if not any((accession, mrn, modality, study_description)):
            raise ValueError(
                "Must specify at least one of: accession, mrn, modality, "
                "or study_description."
            )

        search_params = {
            "name": "",
            "mrn": mrn or "",
            "accNum": accession or "",
            "studyUid": "",
            "studyDescription": study_description or "",
            "modality": normalize_modality(modality),
            "sourceId": source_id,
        }
        date_ranges = build_date_ranges(date_start, date_end, chunk_days)

        exams = self._search_date_ranges(search_params, date_ranges, chunk_days)
        # Remove patientName from exams
        for exam in exams:
            exam.pop("patientName", None)
        exams = apply_inclusion_filter(exams, "modality", exam_modality_inclusion)
        exams = apply_inclusion_filter(exams, "description", exam_description_inclusion)
        exams = apply_exclusion_filter(exams, "modality", exam_modality_exclusion)
        exams = apply_exclusion_filter(exams, "description", exam_description_exclusion)

        if not exams:
            logger.warning("No exams found. Check your search parameters.")
        elif accession and len(exams) > 1:
            logger.info(
                "Accession '%s' matched %d exams. Use filters (-xm, -xd) "
                "to narrow results if needed.",
                accession,
                len(exams),
            )

        return exams

    def _query_data_source(
        self, search_params: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Issue a single query against the data source.

        Args:
            search_params: The complete query payload, including ``dateRange``.

        Returns:
            Tuple of (matching exams, whether the data source truncated them).

        Raises:
            RuntimeError: If the data source reports the query as failed.
        """
        response = self._post(
            "secure/search/query-data-source",
            headers=self._auth_header,
            json=search_params,
        ).json()

        if response.get("successful") is False:
            raise RuntimeError(
                "Search failed. Server message: "
                f"{response.get('message', '(none)')}"
            )

        return (response.get("exams") or []), bool(response.get("truncated"))

    def _search_date_ranges(
        self,
        search_params: dict[str, Any],
        date_ranges: list[dict[str, str]],
        chunk_days: int,
    ) -> list[dict[str, Any]]:
        """Query once per date range and merge the results.

        Chunk boundaries touch, so the same exam can be returned by two
        adjacent queries; duplicates are dropped, keeping first-seen order.

        Args:
            search_params: Query payload without ``dateRange``.
            date_ranges: The ``dateRange`` payloads to query, in order.
            chunk_days: Chunk length, used only for the truncation warning.

        Returns:
            The merged, de-duplicated exams.
        """
        if len(date_ranges) > 1:
            logger.info(
                "Date window exceeds %d day(s); searching in %d chunk(s).",
                chunk_days,
                len(date_ranges),
            )

        exams: list[dict[str, Any]] = []
        seen: set[Any] = set()
        chunks = (
            tqdm(date_ranges, desc="Searching date ranges", leave=False)
            if len(date_ranges) > 1
            else date_ranges
        )

        for date_range in chunks:
            chunk_exams, truncated = self._query_data_source(
                {**search_params, "dateRange": date_range}
            )
            if truncated:
                logger.warning(
                    "The data source truncated results for %s to %s at %d "
                    "exam(s). Re-run with a smaller chunk (e.g. "
                    "--chunk-days %d) to see the rest.",
                    date_range["start"] or "the earliest exam",
                    date_range["end"] or "the latest exam",
                    len(chunk_exams),
                    max(1, chunk_days // 2),
                )
            for exam in chunk_exams:
                key = exam_key(exam)
                if key in seen:
                    continue
                seen.add(key)
                exams.append(exam)

        logger.debug(
            "Search returned %d exam(s) across %d quer%s.",
            len(exams),
            len(date_ranges),
            "y" if len(date_ranges) == 1 else "ies",
        )
        return exams

    def _check_download_started(
        self, download_info: dict[str, Any], project: int
    ) -> bool:
        """Check if a download has started on the server.

        Args:
            download_info: Response from the download start endpoint.
            project: Project ID for the download.

        Returns:
            True if the download has started or completed.

        Raises:
            RuntimeError: If the download initiation failed.
        """
        if "downloadId" not in download_info:
            reason = download_info.get("reason", "")
            if "project" in reason:
                logger.error(
                    "Project ID is invalid or missing. Available projects:"
                )
                for p in self.list_projects():
                    logger.error("  ID: %s, Name: %s", p["id"], p["name"])
            elif "profile" in reason:
                logger.error(
                    "Profile ID is invalid or missing. Available profiles:"
                )
                for p in self.list_profiles():
                    logger.error(
                        "  ID: %s, Name: %s, Description: %s",
                        p["id"],
                        p["name"],
                        p["description"],
                    )
            else:
                logger.error("Unknown error during download initiation.")
            raise RuntimeError(
                f"Download failed. Server response: {download_info}"
            )

        check = self._post(
            "secure/search/download/check",
            headers=self._auth_header,
            json={
                "downloadId": download_info["downloadId"],
                "projectId": project,
            },
        ).json()
        return check["status"] in ("started", "completed")

    def download(
        self,
        accession: str | None = None,
        mrn: str | None = None,
        modality: str | None = None,
        study_description: str | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        chunk_days: int = DEFAULT_CHUNK_DAYS,
        project: int | None = None,
        profile: int | None = None,
        output: Path | None = None,
        exam_modality_inclusion: str | None = None,
        exam_description_inclusion: str | None = None,
        exam_modality_exclusion: str | None = None,
        exam_description_exclusion: str | None = None,
        series_inclusion: str | None = None,
        series_exclusion: str | None = None,
        thinnest_axial: bool = False,
        axial_patterns: str = DEFAULT_AXIAL_PATTERNS,
        search_only: bool = False,
    ) -> list[dict[str, Any]] | None:
        """Search for and download DICOM exams from AIR.

        Supports downloading by accession number (single exam), by MRN (all
        exams for a patient), or by modality and/or study description (all
        matching exams across patients). When ``search_only`` is True, writes
        matching exams to a CSV file without downloading.

        Args:
            accession: Accession number to download.
            mrn: Patient MRN to download exams for.
            modality: Modality code to query the server for (e.g. ``CT``).
            study_description: Study description to query the server for
                (e.g. ``CT ABDOMEN PELVIS W CONTRAST``).
            date_start: Start of the date window, ISO 8601.
            date_end: End of the date window, ISO 8601. Defaults to the
                current time when ``date_start`` is given.
            chunk_days: Maximum span of a single search query, in days.
            project: Project ID. When None, falls back to ``AIR_PROJECT``
                from the credential file or environment.
            profile: Anonymization profile ID. When None, falls back to
                ``AIR_PROFILE`` from the credential file or environment.
            output: Output path (directory or .zip file path).
            exam_modality_inclusion: Comma-separated modality filter patterns.
            exam_description_inclusion: Comma-separated description filter
                patterns.
            exam_modality_exclusion: Comma-separated modality exclusion patterns.
            exam_description_exclusion: Comma-separated description exclusion
                patterns.
            series_inclusion: Comma-separated series description filter
                patterns.
            series_exclusion: Comma-separated series description exclusion
                patterns.
            thinnest_axial: If True, reduce each exam to its structured
                report series plus the single thinnest axial CT series.
                Applied after ``series_inclusion`` / ``series_exclusion``.
            axial_patterns: Comma-separated plane names identifying an axial
                series, matched as whole words in the description.
            search_only: If True, write matching exams to CSV and return
                without downloading.

        Returns:
            List of exam dictionaries if ``search_only`` is True, None
            otherwise.
        """
        # Resolve before searching so a misconfigured AIR_PROJECT or
        # AIR_PROFILE fails now rather than after a long chunked search.
        resolved_project, resolved_profile = -1, -1
        if not search_only:
            resolved_project = self._resolve_project(project)
            resolved_profile = self._resolve_profile(profile)
            if project is None and resolved_project != -1:
                logger.info(
                    "Using project %d from configuration.", resolved_project
                )
            if profile is None and resolved_profile != -1:
                logger.info(
                    "Using anonymization profile %d from configuration.",
                    resolved_profile,
                )

        exams = self.search(
            accession=accession,
            mrn=mrn,
            modality=modality,
            study_description=study_description,
            date_start=date_start,
            date_end=date_end,
            chunk_days=chunk_days,
            exam_modality_inclusion=exam_modality_inclusion,
            exam_description_inclusion=exam_description_inclusion,
            exam_modality_exclusion=exam_modality_exclusion,
            exam_description_exclusion=exam_description_exclusion,
        )

        if not exams:
            return exams if search_only else None

        if search_only:
            if output is not None:
                output.mkdir(parents=True, exist_ok=True)
                write_exams_csv(exams, output, mrn=mrn)
            return exams

        # Default output to current directory if not specified
        if output is None:
            output = Path(".")

        if not accession and not mrn:
            logger.warning(
                "About to download %d exam(s) across multiple patients "
                "(no accession or MRN given). Re-run with --search-only to "
                "preview the list first.",
                len(exams),
            )

        for i, study in tqdm(
            enumerate(exams),
            desc="Downloading exams",
            leave=True,
            total=len(exams),
        ):
            self._download_single_exam(
                study=study,
                exam_index=i,
                output=output,
                project=resolved_project,
                profile=resolved_profile,
                series_inclusion=series_inclusion,
                series_exclusion=series_exclusion,
                thinnest_axial=thinnest_axial,
                axial_patterns=axial_patterns,
            )

        return None

    def _download_single_exam(
        self,
        study: dict[str, Any],
        exam_index: int,
        output: Path | None,
        project: int,
        profile: int,
        series_inclusion: str | None,
        series_exclusion: str | None = None,
        thinnest_axial: bool = False,
        axial_patterns: str = DEFAULT_AXIAL_PATTERNS,
    ) -> None:
        """Download a single exam (study) from the API.

        Args:
            study: The exam/study object from the API.
            exam_index: Index of this exam in the batch.
            output: Base output path.
            project: Project ID.
            profile: Anonymization profile ID.
            series_inclusion: Comma-separated series filter patterns.
            series_exclusion: Comma-separated series exclusion patterns.
            thinnest_axial: If True, keep only the structured reports and the
                thinnest axial CT series.
            axial_patterns: Comma-separated plane names identifying an axial
                series.
        """
        exam_output_fp = build_exam_output_path(output, study, exam_index)

        series = self._post(
            "secure/search/series",
            headers=self._auth_header,
            json=study,
        ).json()

        series = apply_inclusion_filter(series, "description", series_inclusion)
        series = apply_exclusion_filter(series, "description", series_exclusion)
        if thinnest_axial:
            series = select_sr_and_thinnest_axial(series, axial_patterns)
        if not series:
            logger.warning(
                "No series found for %s. Check your search parameters.",
                exam_output_fp.stem,
            )
            return

        # download/start may return non-2xx with a JSON body containing
        # error details (e.g. invalid project/profile), so skip automatic
        # raise and let _check_download_started handle the error.
        download_info = self._post(
            "secure/search/download/start",
            raise_for_status=False,
            headers=self._auth_header,
            json={
                "decompress": False,
                "name": "Download.zip",
                "profile": profile,
                "projectId": project,
                "series": series,
                "study": study,
            },
        ).json()

        while not self._check_download_started(download_info, project):
            time.sleep(0.1)

        download_stream = self._post(
            "secure/search/download/zip",
            headers={"Upgrade-Insecure-Requests": "1"},
            data={
                "params": json.dumps(
                    {
                        "downloadId": download_info["downloadId"],
                        "projectId": project,
                        "name": "Download.zip",
                    }
                ),
                "jwt": self._jwt,
            },
            stream=True,
        )

        total_size = int(download_stream.headers.get("Content-Length", 0))
        with (
            open(exam_output_fp, "wb") as fd,
            tqdm(
                total=total_size,
                unit="B",
                unit_scale=True,
                desc=f"Downloading accession {exam_output_fp.stem}",
                leave=False,
            ) as progress_bar,
        ):
            for chunk in download_stream.iter_content(chunk_size=8192):
                if chunk:
                    fd.write(chunk)
                    progress_bar.update(len(chunk))
