"""Endpoint behaviour, with the provider and Supabase stubbed."""

import pytest

from app.errors import AppError
from app.schemas import SECTION_IDS, GradeSuggestion, SectionGrade
from tests.conftest import CATALOG_FACTS, StubGemini, StubSupabase, make_client


BOARD_ID = "11111111-1111-4111-8111-111111111111"

REAL_ANSWER = {
    "requirements": "Serve product pages under 200ms p99 at four nines. Price edits may lag 60s. Checkout is out of scope.",
    "scale": "8M DAU x 60 requests = 480M/day, /86400 = 5.5k/s average, x3 for peak = about 17k/s.",
    "api": "GET /products/{id} returns the product plus stock; GET /search?q= returns a page of ids.",
    "dataModel": "Product keyed by product_id, partitioned on it because every read is by id.",
    "bottleneck": "The database at 17k/s. Cache-aside on Redis, then read replicas.",
    "tradeoff": "Chose cache-aside over write-through; rejected it because writes are rare.",
}


def suggestion(
    verdicts: list[str], evidence_by_section: dict[str, str] | None = None
) -> GradeSuggestion:
    evidence_by_section = evidence_by_section or REAL_ANSWER
    return GradeSuggestion(
        sections=[
            SectionGrade(
                section=section,
                verdict=verdict,
                evidence="" if verdict == "missing" else evidence_by_section[section],
                gap="What about invalidation?",
            )
            for section, verdict in zip(SECTION_IDS, verdicts, strict=True)
        ],
        hardest_followup="How stale can a replica be before it is wrong?",
    )


def post(client, sections=None, self_rating=None):
    return client.post(
        "/api/v1/ai/grade-talk-track",
        headers={"Authorization": "Bearer token"},
        json={
            "board_id": BOARD_ID,
            "facts": CATALOG_FACTS,
            "sections": sections if sections is not None else REAL_ANSWER,
            "self_rating": self_rating,
        },
    )


class TestGrading:
    def test_returns_a_computed_score_and_the_grade(self):
        gemini = StubGemini(suggestion(["covered"] * 6))
        response = post(make_client(gemini=gemini))
        assert response.status_code == 200
        body = response.json()
        assert body["score"] == 100
        assert body["board_id"] == BOARD_ID
        assert len(body["suggestion"]["sections"]) == 6

    def test_reports_divergence_against_the_self_rating(self):
        gemini = StubGemini(suggestion(["thin"] * 6))
        body = post(make_client(gemini=gemini), self_rating=5).json()
        assert body["score"] == 50
        assert body["divergence"] == 50

    def test_the_self_rating_never_reaches_the_model(self):
        # Anchoring the grader with "they gave themselves 5/5" makes a generous
        # grade more likely, so it is withheld from the prompt entirely.
        gemini = StubGemini(suggestion(["covered"] * 6))
        post(make_client(gemini=gemini), self_rating=5)
        assert "self_rating" not in str(gemini.calls[0]["context"])

    def test_ground_truth_reaches_the_model(self):
        gemini = StubGemini(suggestion(["covered"] * 6))
        post(make_client(gemini=gemini))
        ground_truth = gemini.calls[0]["context"]["ground_truth"]
        assert ground_truth["derived_peak_requests_per_second"] == 16667
        assert ground_truth["derived_storage_gb"] == 1168


PLACEHOLDERS = {
    "requirements": "fast and reliable",
    "scale": "lots of users",
    "api": "REST",
    "dataModel": "postgres",
    "bottleneck": "the db",
    "tradeoff": "cache vs no cache",
}


class TestPlaceholderFloor:
    def test_placeholders_score_zero_even_when_the_model_calls_them_thin(self):
        # The live grader rated exactly these "thin" (50/100); the rubric says missing.
        gemini = StubGemini(suggestion(["thin"] * 6, PLACEHOLDERS))
        body = post(make_client(gemini=gemini), sections=PLACEHOLDERS).json()
        assert body["score"] == 0
        graded = body["suggestion"]["sections"]
        assert [item["verdict"] for item in graded] == ["missing"] * 6
        assert [item["evidence"] for item in graded] == [""] * 6

    @pytest.mark.parametrize(
        "api_text,verdict",
        [
            ("GET /products/{id} returns stock", "missing"),  # four words
            ("GET /products/{id} returns price, stock", "thin"),  # five words
        ],
    )
    def test_the_floor_stops_at_four_words(self, api_text, verdict):
        sections = {**REAL_ANSWER, "api": api_text}
        gemini = StubGemini(suggestion(["thin"] * 6, sections))
        body = post(make_client(gemini=gemini), sections=sections).json()
        api = next(s for s in body["suggestion"]["sections"] if s["section"] == "api")
        assert api["verdict"] == verdict


class TestRefusals:
    def test_rejects_evidence_absent_from_the_submitted_section(self):
        invented = {section: "invented quote" for section in SECTION_IDS}
        gemini = StubGemini(suggestion(["covered"] * 6, invented))
        response = post(make_client(gemini=gemini))
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "invalid_model_response"

    def test_requires_a_bearer_token(self):
        client = make_client()
        response = client.post(
            "/api/v1/ai/grade-talk-track",
            json={
                "board_id": BOARD_ID,
                "facts": CATALOG_FACTS,
                "sections": REAL_ANSWER,
            },
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_required"

    def test_rejects_an_invalid_session(self):
        failing = StubSupabase(
            fail=AppError(401, "authentication_required", "Authentication required.")
        )
        assert post(make_client(supabase=failing)).status_code == 401

    def test_refuses_to_grade_an_empty_talk_track(self):
        blank = {section: "   " for section in SECTION_IDS}
        response = post(make_client(), sections=blank)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "nothing_to_grade"

    def test_surfaces_a_rate_limited_provider(self):
        gemini = StubGemini(fail=AppError(429, "provider_limited", "Rate limited."))
        response = post(make_client(gemini=gemini))
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "provider_limited"

    def test_surfaces_an_ungradeable_model_response(self):
        # What the caller sees when the model breaks the evidence rule: the
        # schema rejects it upstream and this is the resulting error.
        gemini = StubGemini(
            fail=AppError(502, "invalid_model_response", "Invalid response.")
        )
        response = post(make_client(gemini=gemini))
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "invalid_model_response"

    def test_every_error_carries_a_request_id(self):
        gemini = StubGemini(fail=AppError(429, "provider_limited", "Rate limited."))
        assert post(make_client(gemini=gemini)).json()["error"]["request_id"]
