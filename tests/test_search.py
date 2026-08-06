"""Tests for server-side search by modality and study description."""

import pytest

from air_download.client import AIRClient, normalize_modality


@pytest.fixture
def client(tmp_path):
    """Return an authenticated-looking client that makes no real requests."""
    cred_file = tmp_path / "creds.txt"
    cred_file.write_text(
        "AIR_USERNAME=user\nAIR_PASSWORD=pass\nAIR_URL=https://example.com/api/\n"
    )
    client = AIRClient(cred_path=cred_file)
    client._jwt = "fake-jwt"
    return client


@pytest.fixture
def captured_query(client, monkeypatch):
    """Capture the search payload and return a canned response.

    The returned dict is mutated in place: ``payload`` holds the JSON body of
    the last search request, and ``response`` can be reassigned by a test to
    control what the fake server returns.
    """
    state = {
        "payload": None,
        "response": {"successful": True, "truncated": False, "exams": []},
    }

    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(endpoint, raise_for_status=True, **kwargs):
        state["payload"] = kwargs.get("json")
        return FakeResponse(state["response"])

    monkeypatch.setattr(client, "_post", fake_post)
    return state


class TestNormalizeModality:
    """Tests for normalize_modality."""

    def test_none_returns_empty_string(self):
        assert normalize_modality(None) == ""

    def test_empty_returns_empty_string(self):
        assert normalize_modality("") == ""

    def test_uppercases_input(self):
        assert normalize_modality("ct") == "CT"

    def test_strips_whitespace(self):
        assert normalize_modality("  us  ") == "US"

    def test_passes_through_valid_code(self):
        assert normalize_modality("MR") == "MR"

    def test_unknown_code_raises(self):
        with pytest.raises(ValueError, match="Unknown modality 'ZZ'"):
            normalize_modality("ZZ")

    def test_error_lists_accepted_codes(self):
        with pytest.raises(ValueError, match="CT"):
            normalize_modality("computed tomography")


class TestSearchCriteria:
    """Tests for which search criteria the client accepts."""

    def test_no_criteria_raises(self, client):
        with pytest.raises(ValueError, match="at least one of"):
            client.search()

    def test_modality_alone_is_allowed(self, client, captured_query):
        client.search(modality="CT")
        assert captured_query["payload"]["modality"] == "CT"

    def test_study_description_alone_is_allowed(self, client, captured_query):
        client.search(study_description="US ED BEDSIDE")
        assert captured_query["payload"]["studyDescription"] == "US ED BEDSIDE"

    def test_invalid_modality_raises_before_request(self, client, captured_query):
        with pytest.raises(ValueError, match="Unknown modality"):
            client.search(modality="NOPE")
        assert captured_query["payload"] is None


class TestSearchPayload:
    """Tests for the query-data-source request body."""

    def test_sends_all_required_fields(self, client, captured_query):
        client.search(modality="CT", study_description="CT ABDOMEN PELVIS")
        payload = captured_query["payload"]
        # Fields the OpenAPI spec marks as required.
        for field in (
            "name",
            "mrn",
            "accNum",
            "studyUid",
            "studyDescription",
            "modality",
            "sourceId",
            "dateRange",
        ):
            assert field in payload, f"missing required field: {field}"

    def test_modality_is_normalized_in_payload(self, client, captured_query):
        client.search(modality="us")
        assert captured_query["payload"]["modality"] == "US"

    def test_unset_criteria_are_empty_strings(self, client, captured_query):
        client.search(modality="CT")
        payload = captured_query["payload"]
        assert payload["mrn"] == ""
        assert payload["accNum"] == ""
        assert payload["studyUid"] == ""
        assert payload["studyDescription"] == ""

    def test_combines_modality_and_description(self, client, captured_query):
        client.search(
            modality="CT", study_description="CT ABDOMEN PELVIS W CONTRAST"
        )
        payload = captured_query["payload"]
        assert payload["modality"] == "CT"
        assert payload["studyDescription"] == "CT ABDOMEN PELVIS W CONTRAST"


class TestSearchResponseHandling:
    """Tests for how the client interprets the search response."""

    def test_returns_exams(self, client, captured_query):
        captured_query["response"] = {
            "successful": True,
            "exams": [
                {"modality": "CT", "description": "CT ABDOMEN PELVIS", "patientId": "1"}
            ],
        }
        exams = client.search(modality="CT")
        assert len(exams) == 1
        assert exams[0]["description"] == "CT ABDOMEN PELVIS"

    def test_strips_patient_name(self, client, captured_query):
        captured_query["response"] = {
            "successful": True,
            "exams": [{"modality": "CT", "patientName": "TEST^PATIENT"}],
        }
        exams = client.search(modality="CT")
        assert "patientName" not in exams[0]

    def test_missing_exams_key_returns_empty(self, client, captured_query):
        captured_query["response"] = {"successful": True}
        assert client.search(modality="CT") == []

    def test_unsuccessful_response_raises(self, client, captured_query):
        captured_query["response"] = {
            "successful": False,
            "message": "data source unavailable",
        }
        with pytest.raises(RuntimeError, match="data source unavailable"):
            client.search(modality="CT")

    def test_truncated_response_warns(self, client, captured_query, caplog):
        captured_query["response"] = {
            "successful": True,
            "truncated": True,
            "exams": [{"modality": "CT"}],
        }
        with caplog.at_level("WARNING"):
            client.search(modality="CT")
        assert "truncated" in caplog.text.lower()

    def test_client_side_filters_still_apply(self, client, captured_query):
        captured_query["response"] = {
            "successful": True,
            "exams": [
                {"modality": "CT", "description": "CT ABDOMEN PELVIS W CONTRAST"},
                {"modality": "CT", "description": "CT HEAD WO CONTRAST"},
            ],
        }
        exams = client.search(
            modality="CT", exam_description_inclusion="abdomen pelvis"
        )
        assert len(exams) == 1
        assert exams[0]["description"] == "CT ABDOMEN PELVIS W CONTRAST"
