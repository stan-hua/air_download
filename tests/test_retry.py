"""Tests for request retrying and anonymization profile resolution."""

import pytest
import requests

from air_download.client import (
    DEFAULT_MAX_BACKOFF,
    RETRY_STATUS_CODES,
    AIRClient,
    backoff_delay,
)


@pytest.fixture
def cred_file(tmp_path):
    """Write a minimal credential file and return its path."""
    path = tmp_path / "creds.txt"
    path.write_text(
        "AIR_USERNAME=user\nAIR_PASSWORD=pass\nAIR_URL=https://example.com/api/\n"
    )
    return path


@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff delays instead of actually sleeping."""
    delays = []
    monkeypatch.setattr("air_download.client.time.sleep", delays.append)
    return delays


class FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code=200, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body if body is not None else {"successful": True}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def make_session(monkeypatch, client, outcomes):
    """Make the client's session return/raise each outcome in turn.

    Args:
        outcomes: Responses to return, or exceptions to raise, in order. The
            last one repeats once exhausted.

    Returns:
        A list that receives one entry per attempt.
    """
    attempts = []

    def fake_post(url, **kwargs):
        outcome = outcomes[min(len(attempts), len(outcomes) - 1)]
        attempts.append(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(client._session, "post", fake_post)
    return attempts


class TestBackoffDelay:
    """Tests for backoff_delay."""

    def test_delay_doubles_per_attempt(self):
        delays = [backoff_delay(i, backoff_factor=1.0) for i in range(4)]
        assert delays == [1.0, 2.0, 4.0, 8.0]

    def test_backoff_factor_scales_delays(self):
        assert backoff_delay(0, backoff_factor=0.5) == 0.5
        assert backoff_delay(2, backoff_factor=0.5) == 2.0

    def test_delay_is_capped(self):
        assert backoff_delay(20, backoff_factor=1.0) == DEFAULT_MAX_BACKOFF

    def test_numeric_retry_after_wins(self):
        assert backoff_delay(0, retry_after="30", backoff_factor=1.0) == 30.0

    def test_retry_after_is_capped(self):
        assert backoff_delay(0, retry_after="9999") == DEFAULT_MAX_BACKOFF

    def test_negative_retry_after_is_clamped(self):
        assert backoff_delay(0, retry_after="-5") == 0.0

    def test_http_date_retry_after_falls_back_to_exponential(self):
        delay = backoff_delay(
            1, retry_after="Wed, 21 Oct 2015 07:28:00 GMT", backoff_factor=1.0
        )
        assert delay == 2.0


class TestPostRetries:
    """Tests for retry behaviour in AIRClient._post."""

    def test_success_makes_one_attempt(self, cred_file, monkeypatch, no_sleep):
        client = AIRClient(cred_path=cred_file)
        attempts = make_session(monkeypatch, client, [FakeResponse(200)])
        client._post("login", json={})
        assert len(attempts) == 1
        assert no_sleep == []

    def test_retries_then_succeeds(self, cred_file, monkeypatch, no_sleep):
        client = AIRClient(cred_path=cred_file, backoff_factor=1.0)
        attempts = make_session(
            monkeypatch,
            client,
            [FakeResponse(503), FakeResponse(503), FakeResponse(200)],
        )
        response = client._post("login", json={})
        assert response.status_code == 200
        assert len(attempts) == 3
        assert no_sleep == [1.0, 2.0]

    @pytest.mark.parametrize("status", sorted(RETRY_STATUS_CODES))
    def test_transient_statuses_are_retried(
        self, cred_file, monkeypatch, no_sleep, status
    ):
        client = AIRClient(cred_path=cred_file, max_retries=1)
        attempts = make_session(
            monkeypatch, client, [FakeResponse(status), FakeResponse(200)]
        )
        client._post("login", json={})
        assert len(attempts) == 2

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_client_errors_are_not_retried(
        self, cred_file, monkeypatch, no_sleep, status
    ):
        client = AIRClient(cred_path=cred_file)
        attempts = make_session(monkeypatch, client, [FakeResponse(status)])
        with pytest.raises(requests.HTTPError):
            client._post("login", json={})
        assert len(attempts) == 1
        assert no_sleep == []

    def test_connection_errors_are_retried(self, cred_file, monkeypatch, no_sleep):
        client = AIRClient(cred_path=cred_file, max_retries=2)
        attempts = make_session(
            monkeypatch,
            client,
            [requests.ConnectionError("refused"), FakeResponse(200)],
        )
        client._post("login", json={})
        assert len(attempts) == 2

    def test_timeouts_are_retried(self, cred_file, monkeypatch, no_sleep):
        client = AIRClient(cred_path=cred_file, max_retries=2)
        attempts = make_session(
            monkeypatch, client, [requests.Timeout("slow"), FakeResponse(200)]
        )
        client._post("login", json={})
        assert len(attempts) == 2

    def test_exhausted_connection_retries_raise(
        self, cred_file, monkeypatch, no_sleep
    ):
        client = AIRClient(cred_path=cred_file, max_retries=2)
        attempts = make_session(
            monkeypatch, client, [requests.ConnectionError("refused")]
        )
        with pytest.raises(requests.ConnectionError):
            client._post("login", json={})
        assert len(attempts) == 3  # initial attempt plus two retries

    def test_exhausted_status_retries_raise(self, cred_file, monkeypatch, no_sleep):
        client = AIRClient(cred_path=cred_file, max_retries=2)
        attempts = make_session(monkeypatch, client, [FakeResponse(503)])
        with pytest.raises(requests.HTTPError):
            client._post("login", json={})
        assert len(attempts) == 3

    def test_max_retries_zero_disables_retrying(
        self, cred_file, monkeypatch, no_sleep
    ):
        client = AIRClient(cred_path=cred_file, max_retries=0)
        attempts = make_session(monkeypatch, client, [FakeResponse(503)])
        with pytest.raises(requests.HTTPError):
            client._post("login", json={})
        assert len(attempts) == 1
        assert no_sleep == []

    def test_retry_after_header_is_honoured(self, cred_file, monkeypatch, no_sleep):
        client = AIRClient(cred_path=cred_file)
        make_session(
            monkeypatch,
            client,
            [
                FakeResponse(429, headers={"Retry-After": "12"}),
                FakeResponse(200),
            ],
        )
        client._post("login", json={})
        assert no_sleep == [12.0]

    def test_raise_for_status_false_returns_final_error_body(
        self, cred_file, monkeypatch, no_sleep
    ):
        # download/start returns error detail in a non-2xx body.
        client = AIRClient(cred_path=cred_file, max_retries=1)
        make_session(
            monkeypatch,
            client,
            [FakeResponse(400, body={"reason": "invalid project"})],
        )
        response = client._post("start", raise_for_status=False, json={})
        assert response.json() == {"reason": "invalid project"}


class TestResolveProfile:
    """Tests for anonymization profile resolution."""

    def test_argument_wins(self, tmp_path, monkeypatch):
        cred = tmp_path / "creds.txt"
        cred.write_text(
            "AIR_USERNAME=u\nAIR_PASSWORD=p\n"
            "AIR_URL=https://example.com/api/\nAIR_PROFILE=7\n"
        )
        monkeypatch.setenv("AIR_PROFILE", "9")
        assert AIRClient(cred_path=cred)._resolve_profile(3) == 3

    def test_credential_file_beats_environment(self, tmp_path, monkeypatch):
        cred = tmp_path / "creds.txt"
        cred.write_text(
            "AIR_USERNAME=u\nAIR_PASSWORD=p\n"
            "AIR_URL=https://example.com/api/\nAIR_PROFILE=7\n"
        )
        monkeypatch.setenv("AIR_PROFILE", "9")
        assert AIRClient(cred_path=cred)._resolve_profile(None) == 7

    def test_environment_used_without_file_entry(self, cred_file, monkeypatch):
        monkeypatch.setenv("AIR_PROFILE", "9")
        assert AIRClient(cred_path=cred_file)._resolve_profile(None) == 9

    def test_defaults_to_minus_one(self, cred_file, monkeypatch):
        monkeypatch.delenv("AIR_PROFILE", raising=False)
        assert AIRClient(cred_path=cred_file)._resolve_profile(None) == -1

    def test_empty_value_falls_back(self, cred_file, monkeypatch):
        monkeypatch.setenv("AIR_PROFILE", "")
        assert AIRClient(cred_path=cred_file)._resolve_profile(None) == -1

    def test_string_digits_are_accepted(self, cred_file, monkeypatch):
        monkeypatch.setenv("AIR_PROFILE", "42")
        assert AIRClient(cred_path=cred_file)._resolve_profile(None) == 42

    def test_non_integer_raises_with_source(self, cred_file, monkeypatch):
        monkeypatch.setenv("AIR_PROFILE", "default-profile")
        with pytest.raises(ValueError, match="must be an integer"):
            AIRClient(cred_path=cred_file)._resolve_profile(None)

    def test_explicit_minus_one_is_preserved(self, cred_file, monkeypatch):
        monkeypatch.setenv("AIR_PROFILE", "9")
        assert AIRClient(cred_path=cred_file)._resolve_profile(-1) == -1


class TestDownloadUsesConfiguredProfile:
    """Tests that download() applies the resolved profile."""

    @pytest.fixture
    def downloads(self, cred_file, monkeypatch, tmp_path):
        """Return (client, recorded per-exam download kwargs)."""
        client = AIRClient(cred_path=cred_file)
        client._jwt = "fake-jwt"
        monkeypatch.setattr(
            client,
            "search",
            lambda **kwargs: [{"accessionNumber": "111", "studyUid": "1.2.3"}],
        )
        recorded = []
        monkeypatch.setattr(
            client, "_download_single_exam", lambda **kwargs: recorded.append(kwargs)
        )
        return client, recorded, tmp_path

    def test_profile_from_environment_is_used(self, downloads, monkeypatch):
        client, recorded, tmp_path = downloads
        monkeypatch.setenv("AIR_PROFILE", "8")
        client.download(accession="111", output=tmp_path)
        assert recorded[0]["profile"] == 8

    def test_explicit_profile_overrides_environment(self, downloads, monkeypatch):
        client, recorded, tmp_path = downloads
        monkeypatch.setenv("AIR_PROFILE", "8")
        client.download(accession="111", profile=3, output=tmp_path)
        assert recorded[0]["profile"] == 3

    def test_unset_profile_stays_minus_one(self, downloads, monkeypatch):
        client, recorded, tmp_path = downloads
        monkeypatch.delenv("AIR_PROFILE", raising=False)
        client.download(accession="111", output=tmp_path)
        assert recorded[0]["profile"] == -1
