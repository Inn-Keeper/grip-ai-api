"""Poe chat endpoint, with the provider and Supabase stubbed."""

import asyncio

import pytest

from app.errors import AppError
from app.generation import GenerationResult
from app.poe_schemas import PoeReply
from tests.conftest import make_client, settings

URL = "/api/v1/ai/poe/chat"
AUTH = {"Authorization": "Bearer token"}


def ask(text="Where do I practice?"):
    return {"locale": "en", "messages": [{"role": "user", "text": text}]}


def turns(count):
    roles = ["user", "poe"]
    return [{"role": roles[i % 2], "text": f"turn {i}"} for i in range(count)]


class PoeProvider:
    def __init__(self, *, fail: Exception | None = None):
        self.fail = fail
        self.calls = 0
        self.model = None
        self.context = None

    async def generate(self, model, system_prompt, context, output_type):
        self.calls += 1
        self.model, self.context = model, context
        if self.fail:
            raise self.fail
        return GenerationResult(
            value=PoeReply(reply="Try the Story Bank."),
            prompt_tokens=1,
            completion_tokens=1,
            retries=0,
        )


def test_reply_uses_poe_model():
    provider = PoeProvider()
    response = make_client(gemini=provider).post(URL, json=ask(), headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"reply": "Try the Story Bank."}
    assert provider.model == "gemini-3.1-flash-lite"
    assert provider.context["locale"] == "en"


def test_requires_token():
    provider = PoeProvider()
    response = make_client(gemini=provider).post(URL, json=ask())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert provider.calls == 0


def test_ollama_is_unavailable():
    provider = PoeProvider()
    client = make_client(gemini=provider)
    # conftest.settings pins gemini; the service only reads the provider name.
    client.app.state.grade_service.settings.ai_provider = "ollama"
    response = client.post(URL, json=ask(), headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "poe_unavailable"
    assert provider.calls == 0


def test_redacts_before_model():
    provider = PoeProvider()
    make_client(gemini=provider).post(URL, json=ask("mail me at a@b.co"), headers=AUTH)
    assert provider.context["conversation"][-1]["text"] == "mail me at [EMAIL_1]"


def test_sends_only_last_ten_turns():
    provider = PoeProvider()
    body = {"locale": "en", "messages": turns(19)}
    assert (
        make_client(gemini=provider).post(URL, json=body, headers=AUTH).status_code
        == 200
    )
    assert len(provider.context["conversation"]) == 10
    assert provider.context["conversation"][-1]["text"] == "turn 18"


@pytest.mark.parametrize(
    "messages",
    [
        turns(21),
        [{"role": "user", "text": "x" * 501}],
        [{"role": "poe", "text": "x" * 1201}, {"role": "user", "text": "hi"}],
        turns(2),
        [],
    ],
)
def test_rejects_bad_history(messages):
    provider = PoeProvider()
    response = make_client(gemini=provider).post(
        URL, json={"locale": "en", "messages": messages}, headers=AUTH
    )
    assert response.status_code == 422
    assert provider.calls == 0


def test_daily_cap_and_refund():
    provider = PoeProvider(fail=AppError(502, "provider_unavailable", "Down."))
    client = make_client(gemini=provider, ai_poe_daily_cap=1)
    assert client.post(URL, json=ask(), headers=AUTH).status_code == 502
    provider.fail = None
    assert client.post(URL, json=ask(), headers=AUTH).status_code == 200
    capped = client.post(URL, json=ask(), headers=AUTH)
    assert capped.status_code == 429
    assert capped.json()["error"]["code"] == "poe_daily_limit"
    assert int(capped.headers["Retry-After"]) > 0


def test_poe_limit_does_not_block_grading():
    limit = AppError(429, "provider_limited", "Limited.", retry_after=60)
    provider = PoeProvider(fail=limit)
    client = make_client(gemini=provider)
    assert client.post(URL, json=ask(), headers=AUTH).status_code == 429
    assert client.post(URL, json=ask("again"), headers=AUTH).status_code == 429
    assert provider.calls == 1
    assert client.get("/api/v1/ai/status").json()["grading"] == "available"


def test_busy_is_refused_not_queued():
    client = make_client(gemini=PoeProvider(), ai_poe_max_concurrent=1)
    service = client.app.state.poe_service
    with client:
        asyncio.run(service._slots.acquire())
        response = client.post(URL, json=ask(), headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "busy"


def test_rejects_non_strict_poe_model():
    with pytest.raises(ValueError):
        settings(ai_model_poe="gemini-1.0")
