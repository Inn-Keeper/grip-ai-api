"""Claude routing for allowlisted users, and the client's failure modes, offline."""

import json

import anthropic
import httpx2
import pytest

from app.claude import ClaudeClient
from app.errors import AppError
from app.schemas import GradeSuggestion
from tests.conftest import StubGemini, make_client, settings
from tests.test_grade import post, suggestion

GRADE = suggestion(["covered"] * 6)


def test_allowlisted_user_is_graded_by_opus():
    gemini, claude = StubGemini(GRADE), StubGemini(GRADE)
    response = post(
        make_client(gemini=gemini, claude=claude, anthropic_user_ids="user-1")
    )
    assert response.status_code == 200
    assert response.json()["model"] == "claude-opus-5-5"
    assert [call["model"] for call in claude.calls] == ["claude-opus-5-5"]
    assert gemini.calls == []


def test_other_users_stay_on_gemini():
    gemini, claude = StubGemini(GRADE), StubGemini(GRADE)
    response = post(
        make_client(gemini=gemini, claude=claude, anthropic_user_ids="someone-else")
    )
    assert response.json()["model"] == "gemini-3.5-flash"
    assert claude.calls == []


@pytest.mark.parametrize(
    ("use", "model"),
    [
        ("grade", "claude-opus-5-5"),
        ("general", "claude-sonnet-5-5"),
        ("poe", "claude-haiku-5-5"),
    ],
)
def test_model_per_use(use, model):
    service = make_client(
        claude=StubGemini(), anthropic_user_ids="user-1"
    ).app.state.grade_service
    assert service.model_for("user-1", use) == model
    assert service.model_for("user-2", use).startswith("gemini-")


def claude(handler) -> ClaudeClient:
    transport = httpx2.MockTransport(handler)
    return ClaudeClient(
        settings(),
        client=anthropic.AsyncAnthropic(
            api_key="key",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=transport),
        ),
    )


def message(text: str, stop_reason: str = "end_turn"):
    def handler(request):
        handler.sent = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": text}],
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 7},
            },
        )

    return handler


def failure(status: int, kind: str, text: str, headers=None):
    def handler(request):
        return httpx2.Response(
            status,
            headers=headers,
            json={"type": "error", "error": {"type": kind, "message": text}},
        )

    return handler


async def generate(handler, model="claude-opus-5-5"):
    return await claude(handler).generate(model, "system", {}, GradeSuggestion)


async def test_valid_answer_is_parsed_with_schema_and_fallback():
    handler = message(GRADE.model_dump_json())
    result = await generate(handler)
    assert result.value == GRADE
    assert (result.prompt_tokens, result.completion_tokens) == (5, 7)
    assert handler.sent["output_config"]["format"]["type"] == "json_schema"
    assert handler.sent["fallbacks"] == "default"


async def test_haiku_has_no_fallback():
    handler = message(GRADE.model_dump_json())
    await generate(handler, "claude-haiku-5-5")
    assert "fallbacks" not in handler.sent
    assert handler.sent["output_config"]["effort"] == "low"


@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (message("{", "max_tokens"), "response_truncated"),
        (message("", "refusal"), "invalid_model_response"),
        (message('{"sections": []}'), "invalid_model_response"),
        (
            failure(402, "billing_error", "Payment required"),
            "provider_credits_exhausted",
        ),
        (
            failure(400, "invalid_request_error", "Your credit balance is too low."),
            "provider_credits_exhausted",
        ),
        (failure(529, "overloaded_error", "Overloaded"), "provider_unavailable"),
        (failure(401, "authentication_error", "bad key"), "provider_error"),
    ],
)
async def test_failures_keep_distinct_codes(handler, code):
    with pytest.raises(AppError) as raised:
        await generate(handler)
    assert raised.value.code == code


async def test_rate_limit_carries_retry_after():
    handler = failure(429, "rate_limit_error", "slow down", {"retry-after": "12"})
    with pytest.raises(AppError) as raised:
        await generate(handler)
    assert (raised.value.code, raised.value.retry_after) == ("provider_limited", 12)
