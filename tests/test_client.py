"""Tests for air_download.client credential and URL resolution."""

import os

import pytest
import requests

from air_download.client import AIRClient


class TestResolveUrl:
    """Tests for AIRClient URL resolution logic."""

    def test_url_from_argument(self, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text(
            "AIR_USERNAME=user\nAIR_PASSWORD=pass\nAIR_URL=https://file.example.com/api/\n"
        )
        client = AIRClient(url="https://arg.example.com/api/", cred_path=cred_file)
        assert client.url == "https://arg.example.com/api/"

    def test_url_from_credential_file(self, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text(
            "AIR_USERNAME=user\nAIR_PASSWORD=pass\nAIR_URL=https://file.example.com/api/\n"
        )
        client = AIRClient(cred_path=cred_file)
        assert client.url == "https://file.example.com/api/"

    def test_url_from_env_var(self, monkeypatch, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text("AIR_USERNAME=user\nAIR_PASSWORD=pass\n")
        monkeypatch.setenv("AIR_URL", "https://env.example.com/api/")
        client = AIRClient(cred_path=cred_file)
        assert client.url == "https://env.example.com/api/"

    def test_url_trailing_slash_added(self, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text(
            "AIR_USERNAME=user\nAIR_PASSWORD=pass\nAIR_URL=https://example.com/api\n"
        )
        client = AIRClient(cred_path=cred_file)
        assert client.url == "https://example.com/api/"

    def test_url_trailing_slash_preserved(self, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text(
            "AIR_USERNAME=user\nAIR_PASSWORD=pass\nAIR_URL=https://example.com/api/\n"
        )
        client = AIRClient(cred_path=cred_file)
        assert client.url == "https://example.com/api/"

    def test_url_missing_raises_value_error(self, monkeypatch, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text("AIR_USERNAME=user\nAIR_PASSWORD=pass\n")
        monkeypatch.delenv("AIR_URL", raising=False)
        with pytest.raises(ValueError, match="AIR API URL not provided"):
            AIRClient(cred_path=cred_file)

    def test_url_priority_arg_over_file(self, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text(
            "AIR_USERNAME=user\nAIR_PASSWORD=pass\nAIR_URL=https://file.example.com/api/\n"
        )
        client = AIRClient(url="https://arg.example.com/api/", cred_path=cred_file)
        assert client.url == "https://arg.example.com/api/"

    def test_url_priority_file_over_env(self, monkeypatch, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text(
            "AIR_USERNAME=user\nAIR_PASSWORD=pass\nAIR_URL=https://file.example.com/api/\n"
        )
        monkeypatch.setenv("AIR_URL", "https://env.example.com/api/")
        client = AIRClient(cred_path=cred_file)
        assert client.url == "https://file.example.com/api/"


class TestCredentials:
    """Tests for AIRClient credential resolution."""

    def test_credentials_from_file(self, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text(
            "AIR_USERNAME=myuser\nAIR_PASSWORD=mypass\nAIR_URL=https://example.com/api/\n"
        )
        client = AIRClient(cred_path=cred_file)
        username, password = client._get_credentials()
        assert username == "myuser"
        assert password == "mypass"

    def test_credentials_from_env(self, monkeypatch, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text("AIR_URL=https://example.com/api/\n")
        monkeypatch.setenv("AIR_USERNAME", "envuser")
        monkeypatch.setenv("AIR_PASSWORD", "envpass")
        client = AIRClient(cred_path=cred_file)
        username, password = client._get_credentials()
        assert username == "envuser"
        assert password == "envpass"

    def test_missing_credentials_raises(self, monkeypatch, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text("AIR_URL=https://example.com/api/\n")
        monkeypatch.delenv("AIR_USERNAME", raising=False)
        monkeypatch.delenv("AIR_PASSWORD", raising=False)
        client = AIRClient(cred_path=cred_file)
        with pytest.raises(ValueError, match="AIR credentials not provided"):
            client._get_credentials()

    def test_empty_username_raises(self, monkeypatch, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text(
            "AIR_USERNAME=\nAIR_PASSWORD=pass\nAIR_URL=https://example.com/api/\n"
        )
        monkeypatch.delenv("AIR_USERNAME", raising=False)
        monkeypatch.delenv("AIR_PASSWORD", raising=False)
        client = AIRClient(cred_path=cred_file)
        with pytest.raises(ValueError, match="AIR credentials not provided"):
            client._get_credentials()

    def test_empty_password_raises(self, monkeypatch, tmp_path):
        cred_file = tmp_path / "creds.txt"
        cred_file.write_text(
            "AIR_USERNAME=user\nAIR_PASSWORD=\nAIR_URL=https://example.com/api/\n"
        )
        monkeypatch.delenv("AIR_USERNAME", raising=False)
        monkeypatch.delenv("AIR_PASSWORD", raising=False)
        client = AIRClient(cred_path=cred_file)
        with pytest.raises(ValueError, match="AIR credentials not provided"):
            client._get_credentials()

    def test_missing_cred_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="does not exist"):
            AIRClient(
                url="https://example.com/api/",
                cred_path=tmp_path / "nonexistent.txt",
            )

    def test_no_cred_path_uses_env(self, monkeypatch):
        monkeypatch.setenv("AIR_URL", "https://example.com/api/")
        monkeypatch.setenv("AIR_USERNAME", "envuser")
        monkeypatch.setenv("AIR_PASSWORD", "envpass")
        client = AIRClient()
        username, password = client._get_credentials()
        assert username == "envuser"
        assert password == "envpass"


class TestListSeries:
    """Listing an exam's series is a plain query, not part of a download."""

    @staticmethod
    def _client(monkeypatch):
        monkeypatch.setenv("AIR_URL", "https://example.com/api/")
        monkeypatch.setenv("AIR_USERNAME", "envuser")
        monkeypatch.setenv("AIR_PASSWORD", "envpass")
        client = AIRClient()
        # Skip authentication; only the request itself is under test.
        client._jwt = "token"
        return client

    def test_posts_the_study_verbatim_and_returns_the_series(self, monkeypatch):
        client = self._client(monkeypatch)
        series = [{"description": "RUQ", "imageCount": 3, "modality": "US"}]
        calls = []

        class Response:
            def json(self):
                return series

        def fake_post(endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            return Response()

        monkeypatch.setattr(client, "_post", fake_post)
        study = {"accessionNumber": "U1", "studyUid": "1.2.3", "deviceId": 0}
        assert client.list_series(study) == series

        (endpoint, kwargs) = calls[0]
        assert endpoint == "secure/search/series"
        assert kwargs["json"] is study
        # No download endpoint may be touched.
        assert len(calls) == 1


class TestIdentifiersStayOffTheConsole:
    """An accession number is an identifier, so it never reaches a log line."""

    def test_the_no_series_warning_does_not_name_the_accession(
        self, monkeypatch, tmp_path, caplog
    ):
        client = TestListSeries._client(monkeypatch)
        monkeypatch.setattr(client, "list_series", lambda study: [])

        with caplog.at_level("WARNING"):
            client._download_single_exam(
                study={"accessionNumber": "SECRET-ACC"},
                exam_index=0,
                output=tmp_path / "SECRET-ACC.zip",
                project=-1,
                profile=-1,
                series_inclusion=None,
            )

        assert "No series found" in caplog.text
        assert "SECRET-ACC" not in caplog.text

    def test_the_accession_is_still_available_at_debug(
        self, monkeypatch, tmp_path, caplog
    ):
        # Finding which exam missed is a real need; -v is where it belongs.
        client = TestListSeries._client(monkeypatch)
        monkeypatch.setattr(client, "list_series", lambda study: [])

        with caplog.at_level("DEBUG"):
            client._download_single_exam(
                study={"accessionNumber": "SECRET-ACC"},
                exam_index=0,
                output=tmp_path / "SECRET-ACC.zip",
                project=-1,
                profile=-1,
                series_inclusion=None,
            )

        assert "SECRET-ACC" in caplog.text


class TestAnExpiredTokenIsRefreshed:
    """A JWT lasts about half an hour and a cohort download takes days.

    Before this, `_auth_header` authenticated only when `self._jwt` was None,
    nothing re-authenticated on a rejection, and `_post` retried a 401 with
    the same dead token. One run lost 33 of 200 archives that way.
    """

    @staticmethod
    def _client(monkeypatch, tmp_path):
        monkeypatch.setenv("AIR_URL", "https://air.example.com/api/")
        monkeypatch.setenv("AIR_USERNAME", "user")
        monkeypatch.setenv("AIR_PASSWORD", "pass")
        monkeypatch.chdir(tmp_path)
        client = AIRClient(max_retries=0, backoff_factor=0.0)
        client._jwt = "stale"
        return client

    class _Response:
        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

        def json(self):
            return {}

    def _wire(self, client, monkeypatch, codes):
        """Reply with `codes` in order; record what each request carried."""
        sent, logins = [], []

        def fake_authenticate():
            logins.append(client._jwt)
            client._jwt = f"fresh-{len(logins)}"

        def fake_post(url, **kwargs):
            sent.append(kwargs)
            return self._Response(codes[min(len(sent) - 1, len(codes) - 1)])

        monkeypatch.setattr(client, "authenticate", fake_authenticate)
        monkeypatch.setattr(client._session, "post", fake_post)
        return sent, logins

    def test_a_rejected_header_is_replaced_and_the_request_replayed(
        self, monkeypatch, tmp_path
    ):
        client = self._client(monkeypatch, tmp_path)
        sent, logins = self._wire(client, monkeypatch, [401, 200])
        client._post("secure/search/series", headers=client._auth_header, json={})
        assert len(sent) == 2, "the request has to go out again"
        assert len(logins) == 1, "exactly one re-authentication"
        assert sent[0]["headers"]["Authorization"] == "Bearer stale"
        assert sent[1]["headers"]["Authorization"] == "Bearer fresh-1"

    def test_the_download_form_field_is_replaced_too(self, monkeypatch, tmp_path):
        """The zip stream sends its token in the body, not a header, so a fix
        that only rewrote headers would leave the download broken."""
        client = self._client(monkeypatch, tmp_path)
        sent, logins = self._wire(client, monkeypatch, [401, 200])
        client._post("secure/search/download/zip", data={"params": "{}", "jwt": client._jwt})
        assert len(sent) == 2
        assert sent[0]["data"]["jwt"] == "stale"
        assert sent[1]["data"]["jwt"] == "fresh-1"

    def test_a_403_is_treated_the_same_way(self, monkeypatch, tmp_path):
        client = self._client(monkeypatch, tmp_path)
        sent, logins = self._wire(client, monkeypatch, [403, 200])
        client._post("secure/search/series", headers=client._auth_header, json={})
        assert (len(sent), len(logins)) == (2, 1)

    def test_it_re_authenticates_once_and_then_gives_up(self, monkeypatch, tmp_path):
        """A rejection that a new token does not mend must not loop."""
        client = self._client(monkeypatch, tmp_path)
        sent, logins = self._wire(client, monkeypatch, [401])
        with pytest.raises(requests.HTTPError):
            client._post("secure/search/series", headers=client._auth_header, json={})
        assert len(logins) == 1, "one re-authentication, not one per attempt"
        assert len(sent) == 2, "the original and one replay"

    def test_login_never_re_authenticates_itself(self, monkeypatch, tmp_path):
        """`authenticate` posts to `login`, so refreshing there would recurse."""
        client = self._client(monkeypatch, tmp_path)
        sent, logins = self._wire(client, monkeypatch, [401])
        with pytest.raises(requests.HTTPError):
            client._post("login", json={"userId": "u", "password": "p"})
        assert logins == []
        assert len(sent) == 1

    def test_a_request_with_no_token_is_not_replayed(self, monkeypatch, tmp_path):
        """Then the rejection is about something else, and a replay would only
        repeat it at the cost of an extra login."""
        client = self._client(monkeypatch, tmp_path)
        sent, logins = self._wire(client, monkeypatch, [401])
        with pytest.raises(requests.HTTPError):
            client._post("secure/search/series", json={})
        assert logins == []
        assert len(sent) == 1

    def test_a_success_never_re_authenticates(self, monkeypatch, tmp_path):
        client = self._client(monkeypatch, tmp_path)
        sent, logins = self._wire(client, monkeypatch, [200])
        client._post("secure/search/series", headers=client._auth_header, json={})
        assert (len(sent), len(logins)) == (1, 0)

    def test_a_retryable_code_still_backs_off_rather_than_re_authenticating(
        self, monkeypatch, tmp_path
    ):
        client = self._client(monkeypatch, tmp_path)
        sent, logins = self._wire(client, monkeypatch, [503])
        with pytest.raises(requests.HTTPError):
            client._post("secure/search/series", headers=client._auth_header, json={})
        assert logins == []
