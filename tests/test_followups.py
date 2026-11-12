"""Follow-up grading endpoint, with the provider and Supabase stubbed."""

from app.followup_schemas import AnswerGrade, FollowupSuggestion
from tests.conftest import CATALOG_FACTS, StubGemini, make_client


BOARD_ID = "11111111-1111-4111-8111-111111111111"
ANSWER = (
    "Cache-aside on Redis with a 60 second TTL, and an explicit delete on price writes."
)

FOLLOWUPS = [
    {"question": "What's your invalidation strategy?", "answer": ANSWER},
    {"question": "What hits the database on a cold cache?", "answer": ""},
]


def graded(section="q1", verdict="covered", evidence=ANSWER) -> FollowupSuggestion:
    return FollowupSuggestion(
        sections=[
            AnswerGrade(
                section=section,
                verdict=verdict,
                evidence=evidence,
                gap="What if the delete fails?",
            )
        ]
    )


def post(client, followups=None):
    return client.post(
        "/api/v1/ai/grade-followups",
        headers={"Authorization": "Bearer token"},
        json={
            "board_id": BOARD_ID,
            "facts": CATALOG_FACTS,
            "followups": FOLLOWUPS if followups is None else followups,
        },
    )


def test_blank_answers_score_zero_and_never_reach_the_model():
    gemini = StubGemini(graded())
    body = post(make_client(gemini=gemini)).json()
    assert body["score"] == 50
    assert [item["verdict"] for item in body["answers"]] == ["covered", "missing"]
    assert body["answers"][1]["gap"] == FOLLOWUPS[1]["question"]
    sent = gemini.calls[0]["context"]["followup_answers"]
    assert [item["id"] for item in sent] == ["q1"]


def test_fabricated_quote_is_rejected():
    gemini = StubGemini(graded(evidence="Write-through with versioned keys."))
    response = post(make_client(gemini=gemini))
    assert response.status_code == 502


def test_model_must_grade_exactly_the_answers_sent():
    response = post(make_client(gemini=StubGemini(graded(section="q2"))))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_model_response"


def test_all_blank_is_refused_before_the_model():
    gemini = StubGemini(graded())
    response = post(make_client(gemini=gemini), [{"question": "Why?", "answer": " "}])
    assert response.json()["error"]["code"] == "nothing_to_grade"
    assert gemini.calls == []
