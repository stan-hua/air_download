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
    """Capture search payloads and return canned responses.

    The returned dict is mutated in place. ``payload`` holds the JSON body of
    the most recent request and ``payloads`` every body in order, so chunked
    searches can be inspected. ``response`` may be reassigned to a dict, or
    to a callable taking the payload and returning the body, to vary what the
    fake server returns per chunk.
    """
    state = {
        "payload": None,
        "payloads": [],
        "response": {"successful": True, "truncated": False, "exams": []},
    }

    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(endpoint, raise_for_status=True, **kwargs):
        payload = kwargs.get("json")
        state["payload"] = payload
        state["payloads"].append(payload)
        response = state["response"]
        return FakeResponse(response(payload) if callable(response) else response)

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

class TestChunkedSearch:
    """Tests for splitting a long date window across several queries."""

    def test_short_window_is_one_query(self, client, captured_query):
        client.search(modality="CT", date_start="2024-01-01", date_end="2024-01-05")
        assert len(captured_query["payloads"]) == 1

    def test_long_window_is_chunked(self, client, captured_query):
        client.search(modality="CT", date_start="2024-01-01", date_end="2024-01-29")
        # 28 days at 7 days per chunk.
        assert len(captured_query["payloads"]) == 4

    def test_chunks_are_contiguous_and_cover_the_window(self, client, captured_query):
        client.search(modality="CT", date_start="2024-01-01", date_end="2024-02-01")
        ranges = [p["dateRange"] for p in captured_query["payloads"]]
        assert ranges[0]["start"].startswith("2024-01-01")
        assert ranges[-1]["end"].startswith("2024-02-01")
        for earlier, later in zip(ranges, ranges[1:]):
            assert earlier["end"] == later["start"]

    def test_chunk_days_is_configurable(self, client, captured_query):
        client.search(
            modality="CT",
            date_start="2024-01-01",
            date_end="2024-01-11",
            chunk_days=2,
        )
        assert len(captured_query["payloads"]) == 5

    def test_search_criteria_repeat_in_every_chunk(self, client, captured_query):
        client.search(
            modality="CT",
            study_description="CT ABDOMEN PELVIS",
            date_start="2024-01-01",
            date_end="2024-01-29",
        )
        for payload in captured_query["payloads"]:
            assert payload["modality"] == "CT"
            assert payload["studyDescription"] == "CT ABDOMEN PELVIS"

    def test_results_are_merged_across_chunks(self, client, captured_query):
        counter = {"n": 0}

        def respond(payload):
            counter["n"] += 1
            return {
                "successful": True,
                "exams": [{"studyUid": f"uid-{counter['n']}", "modality": "CT"}],
            }

        captured_query["response"] = respond
        exams = client.search(
            modality="CT", date_start="2024-01-01", date_end="2024-01-29"
        )
        assert len(exams) == 4
        assert {e["studyUid"] for e in exams} == {"uid-1", "uid-2", "uid-3", "uid-4"}

    def test_duplicates_across_chunks_are_dropped(self, client, captured_query):
        # An exam sitting on a chunk boundary comes back from both queries.
        captured_query["response"] = {
            "successful": True,
            "exams": [{"studyUid": "same-uid", "modality": "CT"}],
        }
        exams = client.search(
            modality="CT", date_start="2024-01-01", date_end="2024-01-29"
        )
        assert len(exams) == 1

    def test_deduplicates_without_study_uid(self, client, captured_query):
        captured_query["response"] = {
            "successful": True,
            "exams": [
                {"accessionNumber": "111", "dateTime": "2024-01-02T00:00:00-08:00"}
            ],
        }
        exams = client.search(
            modality="CT", date_start="2024-01-01", date_end="2024-01-29"
        )
        assert len(exams) == 1

    def test_truncated_chunk_warns_with_its_dates(self, client, captured_query, caplog):
        captured_query["response"] = {
            "successful": True,
            "truncated": True,
            "exams": [{"studyUid": "uid-1"}],
        }
        with caplog.at_level("WARNING"):
            client.search(modality="CT", date_start="2024-01-01", date_end="2024-01-08")
        assert "truncated" in caplog.text.lower()
        assert "2024-01-01" in caplog.text

    def test_filters_apply_once_after_merging(self, client, captured_query):
        counter = {"n": 0}

        def respond(payload):
            counter["n"] += 1
            return {
                "successful": True,
                "exams": [
                    {
                        "studyUid": f"uid-{counter['n']}",
                        "description": "CT ABDOMEN PELVIS W CONTRAST",
                    },
                    {
                        "studyUid": f"uid-{counter['n']}-b",
                        "description": "CT HEAD WO CONTRAST",
                    },
                ],
            }

        captured_query["response"] = respond
        exams = client.search(
            modality="CT",
            date_start="2024-01-01",
            date_end="2024-01-29",
            exam_description_inclusion="abdomen pelvis",
        )
        assert len(exams) == 4
        assert all("ABDOMEN PELVIS" in e["description"] for e in exams)

    def test_no_dates_sends_empty_range(self, client, captured_query):
        client.search(modality="CT")
        assert captured_query["payload"]["dateRange"] == {
            "start": "",
            "end": "",
            "label": "",
        }

    def test_end_before_start_raises(self, client, captured_query):
        with pytest.raises(ValueError, match="ends before it starts"):
            client.search(
                modality="CT", date_start="2024-02-01", date_end="2024-01-01"
            )
        assert captured_query["payloads"] == []

    def test_unparseable_date_raises(self, client, captured_query):
        with pytest.raises(ValueError, match="Could not parse date"):
            client.search(modality="CT", date_start="last tuesday")
        assert captured_query["payloads"] == []


class TestSearchResponseHandlingContinued:
    """Remaining response-handling tests."""

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
