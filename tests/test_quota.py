"""The free tier is 20 requests a day, so a spent quota must be legible.

Google gives no way to ask how much quota is left: the only signal is the 429
when it runs out. These tests cover reading that signal precisely and then
remembering it, so the next click is refused here instead of spending a request
to be told the same thing again.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.errors import AppError
from app.gemini import GeminiClient, seconds_until_quota_reset
from app.schemas import GradeSuggestion
from tests.conftest import StubGemini, make_client
from tests.test_grade import post, suggestion


# 15:00 UTC is 08:00 Pacific daylight time, 16 hours before the reset.
MORNING = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)

PER_DAY_BODY = """
{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
  {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
    {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
  {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "48s"}]}}
"""

PER_MINUTE_BODY = """
{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
  {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
    {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
  {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "48s"}]}}
"""


def limited(body: str, clock=lambda: MORNING) -> GeminiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text=body)

    return GeminiClient(
        Settings(_env_file=None, ai_provider="gemini", gemini_api_key="key"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=clock,
    )


async def raises(client: GeminiClient) -> AppError:
    with pytest.raises(AppError) as raised:
        await client.generate("gemini-3.5-flash", "system", {}, GradeSuggestion)
    return raised.value


def test_the_reset_is_the_next_midnight_pacific():
    assert seconds_until_quota_reset(MORNING) == 16 * 3600


def test_the_reset_is_never_in_the_past():
    just_before = datetime(2026, 9, 22, 6, 59, 59, tzinfo=timezone.utc)
    assert seconds_until_quota_reset(just_before) > 0


async def test_a_spent_daily_quota_is_told_apart_from_a_rate_limit():
    error = await raises(limited(PER_DAY_BODY))
    assert error.code == "provider_quota_exhausted"
    # Google's 48s retryDelay is meaningless for a daily quota.
    assert error.retry_after == 16 * 3600


async def test_a_per_minute_limit_uses_the_provider_s_own_delay():
    error = await raises(limited(PER_MINUTE_BODY))
    assert error.code == "provider_limited"
    assert error.retry_after == 48


async def test_a_read_timeout_is_not_retried_because_google_may_have_counted_it():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise httpx.ReadTimeout("too slow", request=request)

    client = GeminiClient(
        Settings(
            _env_file=None, ai_provider="gemini", gemini_api_key="key", ai_max_retries=3
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert (await raises(client)).code == "provider_unavailable"
    assert len(sent) == 1


async def test_a_connection_failure_is_retried_because_it_never_reached_google():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise httpx.ConnectError("refused", request=request)

    client = GeminiClient(
        Settings(
            _env_file=None, ai_provider="gemini", gemini_api_key="key", ai_max_retries=2
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=lambda _: _noop(),
    )
    assert (await raises(client)).code == "provider_unavailable"
    assert len(sent) == 3


async def _noop() -> None:
    return None


class TestRememberingASpentQuota:
    """After the first 429, the answer comes from memory, not from Google."""

    def exhausted(self, clock):
        gemini = StubGemini(
            fail=AppError(
                429,
                "provider_quota_exhausted",
                "Used up.",
                retry_after=3600,
            )
        )
        return gemini, make_client(gemini=gemini, clock=clock)

    def test_a_second_attempt_does_not_reach_the_provider(self):
        gemini, client = self.exhausted(lambda: MORNING)

        first, second = post(client), post(client)

        assert first.status_code == second.status_code == 429
        assert second.json()["error"]["code"] == "provider_quota_exhausted"
        assert len(gemini.calls) == 1

    def test_the_response_says_when_to_come_back(self):
        _, client = self.exhausted(lambda: MORNING)
        assert post(client).headers["Retry-After"] == "3600"

    def test_grading_resumes_once_the_quota_resets(self):
        now = MORNING
        gemini, client = self.exhausted(lambda: now)
        post(client)

        now = MORNING + timedelta(seconds=3601)
        gemini.fail = None
        gemini.value = suggestion(["thin"] * 6)
        assert post(client).status_code == 200
        assert len(gemini.calls) == 2

    def test_status_reports_the_block_without_spending_a_request(self):
        gemini, client = self.exhausted(lambda: MORNING)

        assert client.get("/api/v1/ai/status").json() == {"grading": "available"}
        post(client)
        blocked = client.get("/api/v1/ai/status").json()

        assert blocked["grading"] == "unavailable"
        assert blocked["code"] == "provider_quota_exhausted"
        assert blocked["retry_after"] == 3600
        assert len(gemini.calls) == 1
