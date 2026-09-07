"""Exercise the real adapter and application wiring with HTTP transport stubs."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.errors import AppError
from app.main import create_app
from app.schemas import GradeSuggestion
from tests.conftest import CATALOG_FACTS
from tests.test_grade import REAL_ANSWER, suggestion


def config(**overrides):
    return Settings(_env_file=None, ai_provider="ollama", **overrides)


def response_payload(**overrides):
    return {
        "done": True,
        "done_reason": "stop",
        "message": {
            "role": "assistant",
            "content": suggestion(["thin"] * 6).model_dump_json(),
        },
        "prompt_eval_count": 123,
        "eval_count": 45,
        **overrides,
    }


async def run_adapter(handler, **settings):
    from app.ollama import OllamaClient

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        return await OllamaClient(config(**settings), http_client=http).generate(
            "grip-grader", "rubric", {"answer": "hello"}, GradeSuggestion
        )


async def test_native_schema_request_and_validated_response():
    def handler(request):
        assert str(request.url) == "http://localhost:11434/api/chat"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body["model"] == "grip-grader"
        assert body["stream"] is False
        assert body["think"] is False
        assert body["keep_alive"] == 0
        assert body["format"]["properties"]["sections"]["minItems"] == 6
        assert body["options"]["num_ctx"] == 8192
        assert body["options"]["num_predict"] == 2048
        assert body["messages"][0] == {"role": "system", "content": "rubric"}
        assert '"answer":"hello"' in body["messages"][1]["content"]
        return httpx.Response(200, json=response_payload())

    result = await run_adapter(handler)
    assert len(result.value.sections) == 6
    assert result.value.sections[0].verdict == "thin"
    assert (result.prompt_tokens, result.completion_tokens) == (123, 45)


@pytest.mark.parametrize(
    "payload,code",
    [
        (response_payload(done_reason="length"), "response_truncated"),
        (response_payload(done=False), "response_truncated"),
        (response_payload(message={"content": "{}"}), "invalid_model_response"),
        (response_payload(message={"content": "not json"}), "invalid_model_response"),
        ([], "invalid_model_response"),
    ],
)
async def test_bad_output_is_not_returned_as_a_grade(payload, code):
    with pytest.raises(AppError) as error:
        await run_adapter(lambda request: httpx.Response(200, json=payload))
    assert error.value.code == code


@pytest.mark.parametrize(
    "status,code",
    [
        (404, "model_not_found"),
        (429, "provider_limited"),
        (500, "provider_unavailable"),
        (400, "provider_error"),
    ],
)
async def test_http_failures_have_actionable_errors(status, code):
    with pytest.raises(AppError) as error:
        await run_adapter(
            lambda request: httpx.Response(status, json={"error": "failure"})
        )
    assert error.value.code == code


async def test_timeout_is_reported_without_repeating_expensive_generation():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(AppError) as error:
        await run_adapter(handler)
    assert error.value.code == "provider_unavailable"
    assert calls == 1


async def test_oversized_context_never_reaches_ollama():
    def handler(request):
        pytest.fail("Oversized context was sent to Ollama")

    with pytest.raises(AppError) as error:
        await run_adapter(handler, ai_context_max_chars=1)
    assert error.value.status_code == 413


async def test_concurrent_grades_are_serialized_for_local_memory_budget():
    from app.ollama import OllamaClient

    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, json=response_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OllamaClient(config(), http_client=http)
        first = asyncio.create_task(
            client.generate("grip-grader", "rubric", {}, GradeSuggestion)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(
            client.generate("grip-grader", "rubric", {}, GradeSuggestion)
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
    assert peak == 1


@pytest.mark.parametrize(
    "options",
    [
        {"ollama_url": "localhost:11434"},
        {"ollama_context_length": 8192, "ai_max_output_tokens": 8192},
    ],
)
def test_invalid_local_configuration_fails_at_startup(options):
    with pytest.raises(ValidationError):
        config(**options)


def test_ollama_accepts_local_model_names_without_a_gemini_key():
    settings = config(ai_model_grade="my-local-model")
    client = TestClient(create_app(settings))
    assert "GEMINI_API_KEY" not in client.get("/ready").json()["missing"]


def test_gemini_still_rejects_unsupported_models():
    with pytest.raises(ValidationError, match="strict outputs"):
        Settings(_env_file=None, ai_provider="gemini", ai_model_grade="my-local-model")


def test_lifespan_wires_ollama_into_authenticated_grading(monkeypatch):
    # Intercept HTTP only: exercise real auth gateway, provider, service and route.
    real_client = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/auth/v1/user":
            assert request.headers["authorization"] == "Bearer session"
            return httpx.Response(200, json={"id": "user-1"})
        assert request.url.path == "/api/chat"
        return httpx.Response(200, json=response_payload())

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)),
    )
    settings = config(
        supabase_url="https://example.supabase.co", supabase_anon_key="anon"
    )
    with TestClient(create_app(settings)) as client:
        result = client.post(
            "/api/v1/ai/grade-talk-track",
            headers={"Authorization": "Bearer session"},
            json={
                "board_id": "11111111-1111-4111-8111-111111111111",
                "facts": CATALOG_FACTS,
                "sections": REAL_ANSWER,
            },
        )
    assert result.status_code == 200
    assert result.json()["model"] == "grip-grader"
    assert result.json()["score"] == 50
    assert calls == ["/auth/v1/user", "/api/chat"]
