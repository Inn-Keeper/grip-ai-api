import pytest
from uuid import UUID
from tests.conftest import make_client


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/v1/ai/grade-talk-track", {"board_id": "PRIVATE_SENTINEL"}),
        ("/api/v1/ai/import/parse", {}),
        ("/api/v1/ai/import/postings", {"urls": []}),
    ],
)
def test_validation_has_safe_error_envelope(path, body):
    response = make_client().post(path, json=body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert UUID(error["request_id"])
    assert "PRIVATE_SENTINEL" not in response.text
    assert "input" not in error


def test_malformed_json_is_normalized():
    response = make_client().post(
        "/api/v1/ai/import/parse",
        content="PRIVATE_SENTINEL",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "PRIVATE_SENTINEL" not in response.text


@pytest.mark.parametrize(
    "patch",
    [
        {"board_id": "bad-uuid"},
        {"sections": {"PRIVATE_SENTINEL": "PRIVATE_SENTINEL"}},
        {"sections": {"api": "PRIVATE_SENTINEL" * 1000}},
    ],
)
def test_grade_input_errors_do_not_echo_values_or_dictionary_keys(patch, caplog):
    from tests.test_grade import BOARD_ID, REAL_ANSWER
    from tests.conftest import CATALOG_FACTS

    body = {
        "board_id": BOARD_ID,
        "facts": CATALOG_FACTS,
        "sections": REAL_ANSWER,
        **patch,
    }
    response = make_client().post("/api/v1/ai/grade-talk-track", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "PRIVATE_SENTINEL" not in response.text + caplog.text


def test_section_limit_error_explains_how_to_recover():
    from tests.test_grade import BOARD_ID
    from tests.conftest import CATALOG_FACTS

    response = make_client().post(
        "/api/v1/ai/grade-talk-track",
        json={
            "board_id": BOARD_ID,
            "facts": CATALOG_FACTS,
            "sections": {"api": "x" * 8001},
        },
    )
    assert "8,000" in response.json()["error"]["message"]


def test_import_source_conflict_explains_how_to_recover():
    response = make_client().post(
        "/api/v1/ai/import/parse",
        json={"text": "private", "filename": "notes.md", "content_base64": "eA=="},
    )
    assert "either text or" in response.json()["error"]["message"]
