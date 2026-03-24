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

from air_download.filters import apply_exclusion_filter, apply_inclusion_filter
from air_download.utils import build_exam_output_path, write_exams_csv

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_ID = 1


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

    Args:
        url: AIR API base URL. If not provided, resolved from credential
            file or ``AIR_URL`` environment variable.
        cred_path: Path to a dotenv-style credential file containing
            ``AIR_USERNAME``, ``AIR_PASSWORD``, and optionally ``AIR_URL``.
            If None, credentials are read from environment variables.
    """

    def __init__(
        self, url: str | None = None, cred_path: str | Path | None = None
    ) -> None:
        self._cred_path = Path(cred_path) if cred_path else None
        self._envs = self._load_credential_file()
        self.url = self._resolve_url(url)
        self._session = requests.Session()
        self._jwt: str | None = None
        self._projects: list[dict[str, Any]] | None = None

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
        """Make a POST request to the API with error handling.

        Args:
            endpoint: API endpoint path (appended to the base URL).
            raise_for_status: If True (default), raise an HTTPError for
                non-2xx responses. Set to False for endpoints that return
                non-2xx status codes with useful JSON error bodies.
            **kwargs: Additional keyword arguments passed to ``requests.post``.

        Returns:
            The response object.

        Raises:
            requests.HTTPError: If ``raise_for_status`` is True and the
                response status code indicates an error.
        """
        response = self._session.post(urljoin(self.url, endpoint), **kwargs)
        if raise_for_status:
            response.raise_for_status()
        return response

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
        exam_modality_inclusion: str | None = None,
        exam_description_inclusion: str | None = None,
        exam_modality_exclusion: str | None = None,
        exam_description_exclusion: str | None = None,
        source_id: int = DEFAULT_SOURCE_ID,
    ) -> list[dict[str, Any]]:
        """Search for exams by accession number or MRN.

        Args:
            accession: Accession number to search for.
            mrn: Patient MRN to search for.
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
            ValueError: If neither accession nor mrn is provided.
        """
        if not accession and not mrn:
            raise ValueError("Must specify either accession or mrn.")

        search_params = {
            "name": "",
            "mrn": mrn or "",
            "accNum": accession or "",
            "dateRange": {"start": "", "end": "", "label": ""},
            "modality": "",
            "sourceId": source_id,
        }
        response = self._post(
            "secure/search/query-data-source",
            headers=self._auth_header,
            json=search_params,
        ).json()

        exams = response["exams"]
        logger.debug("Search returned %d exam(s).", len(exams))
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
        project: int = -1,
        profile: int = -1,
        output: Path | None = None,
        exam_modality_inclusion: str | None = None,
        exam_description_inclusion: str | None = None,
        exam_modality_exclusion: str | None = None,
        exam_description_exclusion: str | None = None,
        series_inclusion: str | None = None,
        series_exclusion: str | None = None,
        search_only: bool = False,
    ) -> list[dict[str, Any]] | None:
        """Search for and download DICOM exams from AIR.

        Supports downloading by accession number (single exam) or by MRN
        (all exams for a patient). When ``search_only`` is True, writes
        matching exams to a CSV file without downloading.

        Args:
            accession: Accession number to download.
            mrn: Patient MRN to download exams for.
            project: Project ID.
            profile: Anonymization profile ID.
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
            search_only: If True, write matching exams to CSV and return
                without downloading.

        Returns:
            List of exam dictionaries if ``search_only`` is True, None
            otherwise.
        """
        exams = self.search(
            accession=accession,
            mrn=mrn,
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
                project=project,
                profile=profile,
                series_inclusion=series_inclusion,
                series_exclusion=series_exclusion,
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
        """
        exam_output_fp = build_exam_output_path(output, study, exam_index)

        series = self._post(
            "secure/search/series",
            headers=self._auth_header,
            json=study,
        ).json()

        series = apply_inclusion_filter(series, "description", series_inclusion)
        series = apply_exclusion_filter(series, "description", series_exclusion)
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
