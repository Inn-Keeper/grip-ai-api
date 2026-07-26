import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas import GradeSuggestion, ScenarioFacts
from app.supabase import Session


def settings() -> Settings:
    return Settings(
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon",
        gemini_api_key="key",
        allowed_origins="http://localhost:5173",
    )


class StubSupabase:
    """Accepts any token; auth itself is exercised in test_auth.py."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail

    async def validate_session(self, token: str) -> Session:
        if self.fail:
            raise self.fail
        return Session(user_id="user-1", token=token)


class StubGemini:
    """Returns a canned suggestion, or raises, without touching the network."""

    def __init__(
        self, value: GradeSuggestion | None = None, *, fail: Exception | None = None
    ):
        self.value = value
        self.fail = fail
        self.calls: list[dict] = []

    async def generate(self, model, system_prompt, context, output_type):
        self.calls.append({"model": model, "system": system_prompt, "context": context})
        if self.fail:
            raise self.fail

        from app.gemini import GeminiResult

        return GeminiResult(
            value=self.value, prompt_tokens=10, completion_tokens=10, retries=0
        )


# The read-heavy catalog scenario, with figures matching packages/core.
CATALOG_FACTS = {
    "scenario_id": "catalog",
    "name": "Read-heavy product catalog",
    "brief": "Millions of reads, few writes. Serve product pages fast and cheap.",
    "dau": 8_000_000,
    "payload_kb": 4.0,
    "retention_days": 1825,
    "peak_qps": 16666.7,
    "storage_gb": 1168.0,
    "checks_passed": ["A CDN exists for static assets"],
    "checks_failed": ["Hot reads come from a cache"],
    "node_types": ["client", "cdn", "service", "sql"],
    "partition_keys": [],
    "pushback": "A merchandiser edits a price. How long until every shopper sees it?",
}


@pytest.fixture
def facts() -> ScenarioFacts:
    return ScenarioFacts(**CATALOG_FACTS)


def make_client(supabase=None, gemini=None) -> TestClient:
    from app.service import GradeService

    config = settings()
    service = GradeService(
        config,
        supabase or StubSupabase(),
        gemini or StubGemini(),
    )
    return TestClient(create_app(config, grade_service=service))
