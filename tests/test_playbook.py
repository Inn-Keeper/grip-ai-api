"""Playbook endpoint, with the provider and Supabase stubbed."""

import asyncio

from app.errors import AppError
from app.generation import GenerationResult
from app.playbook_schemas import ModelPlaybook
from tests.conftest import make_client
from tests.test_playbook_schemas import check, story

URL = "/api/v1/ai/first-run/playbook"
AUTH = {"Authorization": "Bearer token"}


def canned(competency="Failure") -> ModelPlaybook:
    return ModelPlaybook.model_validate(
        {
            "stories": [story(competency=competency)] + [story()] * 4,
            "scenario": {
                "name": "Payments",
                "brief": "Design a payments service.",
                "checks": [
                    check(),
                    check(kind="edge", type=[], **{"from": ["client"]}, to=["service"]),
                    check(type=["sql"]),
                ],
            },
        }
    )


class PlaybookProvider:
    def __init__(self, value=None, *, fail: Exception | None = None, gate=None):
        self.value = value or canned()
        self.fail = fail
        self.gate = gate
        self.calls = 0

    async def generate(self, model, system_prompt, context, output_type):
        self.calls += 1
        if self.gate:
            await self.gate.wait()
        if self.fail:
            raise self.fail
        return GenerationResult(
            value=self.value, prompt_tokens=1, completion_tokens=1, retries=0
        )


def body(**over) -> dict:
    base = {
        "stack": ["React"],
        "goal": "stories",
        "role": "Backend Engineer",
        "competencies": ["Conflict", "Failure", "Impact"],
        "node_types": ["client", "service", "sql", "cache"],
    }
    return {**base, **over}


def test_requires_token():
    response = make_client(gemini=PlaybookProvider()).post(URL, json=body())
    assert response.status_code == 401


def test_returns_validated_playbook():
    response = make_client(gemini=PlaybookProvider()).post(
        URL, json=body(), headers=AUTH
    )
    assert response.status_code == 200
    checks = response.json()["scenario"]["checks"]
    assert checks[1]["from"] == ["client"]
    assert len(response.json()["stories"]) == 5


def test_invalid_model_output_is_502():
    provider = PlaybookProvider(canned(competency="Teamwork"))
    response = make_client(gemini=provider).post(URL, json=body(), headers=AUTH)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_model_response"


def test_same_input_hits_cache():
    provider = PlaybookProvider()
    client = make_client(gemini=provider)
    client.post(URL, json=body(), headers=AUTH)
    second = client.post(URL, json=body(), headers=AUTH)
    assert second.status_code == 200
    assert provider.calls == 1


def test_daily_cap():
    client = make_client(gemini=PlaybookProvider(), ai_playbook_daily_cap=1)
    assert client.post(URL, json=body(), headers=AUTH).status_code == 200
    second = client.post(URL, json=body(role="Data Engineer"), headers=AUTH)
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "playbook_daily_limit"
    assert int(second.headers["Retry-After"]) > 0


def test_busy_when_slots_full():
    client = make_client(gemini=PlaybookProvider(), ai_max_concurrent=1)
    service = client.app.state.playbook_service
    with client:
        # Hold the only slot, as an in-flight generation would.
        asyncio.run(service._slots.acquire())
        response = client.post(URL, json=body(), headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "busy"
    assert response.headers["Retry-After"] == "30"


def test_remembered_limit_blocks_without_calling():
    limit = AppError(429, "provider_limited", "Limited.", retry_after=60)
    provider = PlaybookProvider(fail=limit)
    client = make_client(gemini=provider)
    assert client.post(URL, json=body(), headers=AUTH).status_code == 429
    again = client.post(URL, json=body(role="Other"), headers=AUTH)
    assert again.status_code == 429
    assert provider.calls == 1


def test_status_reports_provider():
    assert make_client().get("/api/v1/ai/status").json()["provider"] == "gemini"


def test_failed_generation_refunds_the_cap():
    limit = AppError(502, "provider_unavailable", "Down.")
    provider = PlaybookProvider(fail=limit)
    client = make_client(gemini=provider, ai_playbook_daily_cap=1)
    assert client.post(URL, json=body(), headers=AUTH).status_code == 502
    provider.fail = None
    assert client.post(URL, json=body(), headers=AUTH).status_code == 200


def test_invalid_output_refunds_the_cap():
    provider = PlaybookProvider(canned(competency="Teamwork"))
    client = make_client(gemini=provider, ai_playbook_daily_cap=1)
    assert client.post(URL, json=body(), headers=AUTH).status_code == 502
    provider.value = canned()
    assert client.post(URL, json=body(), headers=AUTH).status_code == 200
