"""Story grading endpoint, with the provider and Supabase stubbed."""

from app.story_schemas import (
    STORY_SECTION_IDS,
    StoryGradeSuggestion,
    StorySectionGrade,
)
from tests.conftest import StubGemini, make_client


STORY_ID = "22222222-2222-4222-8222-222222222222"

STORY = {
    "situation": "Checkout errors doubled a week before the holiday sale and nobody owned the service.",
    "task": "I was asked to find the cause and get the error rate back under one percent.",
    "action": "I traced the failures to a retry storm, added jitter and a circuit breaker, and paired with payments on a rollout.",
    "result": "Errors fell from 4% to 0.3% in two days and the sale ran without an incident.",
}


def suggestion(verdicts, evidence=None) -> StoryGradeSuggestion:
    evidence = evidence or STORY
    return StoryGradeSuggestion(
        sections=[
            StorySectionGrade(
                section=section,
                verdict=verdict,
                evidence="" if verdict == "missing" else evidence[section],
                gap="What would you do differently?",
            )
            for section, verdict in zip(STORY_SECTION_IDS, verdicts, strict=True)
        ],
        hardest_followup="How do you know the fix caused the drop?",
    )


def post(client, sections=None, token="token"):
    return client.post(
        "/api/v1/ai/grade-story",
        headers={"Authorization": f"Bearer {token}"} if token else {},
        json={
            "story_id": STORY_ID,
            "competency": "Delivery",
            "title": "Holiday checkout fix",
            "sections": STORY if sections is None else sections,
        },
    )


def test_score_is_computed_from_verdicts():
    gemini = StubGemini(suggestion(["covered", "covered", "thin", "missing"]))
    response = post(make_client(gemini=gemini))
    assert response.status_code == 200
    body = response.json()
    assert body["score"] == 62  # (100 + 100 + 50 + 0) / 4, rounded
    assert body["story_id"] == STORY_ID
    assert gemini.calls[0]["context"]["candidate_sections"] == STORY


def test_fabricated_quote_is_rejected():
    fake = {**STORY, "result": "Revenue tripled."}
    response = post(make_client(gemini=StubGemini(suggestion(["covered"] * 4, fake))))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_model_response"


def test_placeholder_section_scores_missing_whatever_the_model_says():
    sections = {**STORY, "result": "It went well"}
    gemini = StubGemini(suggestion(["covered"] * 4, sections))
    body = post(make_client(gemini=gemini), sections).json()
    assert body["score"] == 75
    assert body["suggestion"]["sections"][3]["verdict"] == "missing"


def test_empty_story_and_missing_token_never_reach_the_model():
    gemini = StubGemini(suggestion(["covered"] * 4))
    client = make_client(gemini=gemini)
    assert post(client, {}).json()["error"]["code"] == "nothing_to_grade"
    assert post(client, token=None).status_code == 401
    assert gemini.calls == []


def test_unchanged_story_is_answered_from_cache():
    gemini = StubGemini(suggestion(["covered"] * 4))
    client = make_client(gemini=gemini)
    post(client)
    post(client)
    assert len(gemini.calls) == 1
