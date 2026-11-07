"""Unchanged reasoning is graded once, so resubmitting it costs no quota."""

from app.errors import AppError
from app.supabase import Session
from tests.conftest import StubGemini, make_client
from tests.test_grade import REAL_ANSWER, post, suggestion


class TokenIsUser:
    """Each bearer token is its own user, so one client can act as several."""

    async def validate_session(self, token: str) -> Session:
        return Session(user_id=token, token=token)


def test_resubmitting_unchanged_reasoning_does_not_call_the_model_again():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(gemini=gemini)

    first, second = post(client).json(), post(client).json()

    assert len(gemini.calls) == 1
    assert first["score"] == second["score"] == 50
    assert first["request_id"] != second["request_id"]


def test_edited_reasoning_is_graded_again():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(gemini=gemini)
    edited = {**REAL_ANSWER, "tradeoff": REAL_ANSWER["tradeoff"] + " Revisit at 10x."}

    post(client)
    post(client, sections=edited)

    assert len(gemini.calls) == 2


def test_grades_are_not_shared_between_users():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(supabase=TokenIsUser(), gemini=gemini)

    post(client, token="alice")
    post(client, token="bob")

    assert len(gemini.calls) == 2


def test_failures_are_not_cached():
    gemini = StubGemini(fail=AppError(429, "provider_limited", "Rate limited."))
    client = make_client(gemini=gemini)

    post(client)
    post(client)

    assert len(gemini.calls) == 2


def test_the_least_recently_used_grade_is_evicted_first():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(supabase=TokenIsUser(), gemini=gemini, ai_grade_cache_size=2)

    for token in ("alice", "bob", "alice", "carol", "alice"):
        post(client, token=token)

    # alice was reused before carol arrived, so bob's grade made room.
    assert len(gemini.calls) == 3


def test_a_zero_size_cache_always_calls_the_model():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(gemini=gemini, ai_grade_cache_size=0)

    post(client)
    post(client)

    assert len(gemini.calls) == 2


def test_cached_grade_survives_a_remembered_provider_limit():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(gemini=gemini)
    first = post(client).json()
    gemini.fail = AppError(429, "provider_quota_exhausted", "Used up", retry_after=3600)
    edited = {**REAL_ANSWER, "api": REAL_ANSWER["api"] + " Another operation."}
    assert post(client, sections=edited).status_code == 429
    cached = post(client)
    assert cached.status_code == 200
    assert cached.json()["suggestion"] == first["suggestion"]
    assert cached.json()["request_id"] != first["request_id"]
    assert len(gemini.calls) == 2


def test_cached_grade_still_obeys_a_lowered_context_limit():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(gemini=gemini)
    assert post(client).status_code == 200
    client.app.state.grade_service.settings.ai_context_max_chars = 1
    response = post(client)
    assert response.status_code == 413
    assert len(gemini.calls) == 1


def test_cache_recomputes_self_rating_without_spending_a_call():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(gemini=gemini)
    assert post(client, self_rating=5).json()["divergence"] == 50
    assert post(client, self_rating=1).json()["divergence"] == -30
    assert len(gemini.calls) == 1
