"""Gemini client, adapted from ativscrum-ai-api.

Unchanged in substance: strict JSON schema output, retry with jitter on
transport and 5xx only, and a typed error for every distinct failure so the
caller can tell "rate limited" from "returned nonsense".
"""

import asyncio
import json
import random as random_module
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta, timezone
from typing import TypeVar
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.errors import AppError
from app.generation import GenerationResult


T = TypeVar("T", bound=BaseModel)

GEMINI_OPENAI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)

# Free-tier requests-per-day quotas reset at midnight Pacific time.
QUOTA_RESET_ZONE = ZoneInfo("America/Los_Angeles")
RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+)')
DEFAULT_RETRY_AFTER_SECONDS = 60
# Failures where the request never reached Gemini, so a retry spends no quota.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def seconds_until_quota_reset(now: datetime) -> int:
    local = now.astimezone(QUOTA_RESET_ZONE)
    midnight = datetime.combine(
        local.date() + timedelta(days=1), time(), QUOTA_RESET_ZONE
    )
    # Subtract in UTC: same-zone arithmetic ignores a DST change in between.
    remaining = midnight.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    return max(1, int(remaining.total_seconds()))


def rate_limit_error(body: str, now: datetime, retries: int) -> AppError:
    """Tell a spent daily quota apart from a per-minute limit.

    Gemini names the exhausted quota in the 429 body, for example
    GenerateRequestsPerDayPerProjectPerModel-FreeTier. Its retryDelay only
    means something for per-minute limits; a daily quota returns at midnight
    Pacific time.
    """
    if "PerDay" in body:
        return AppError(
            429,
            "provider_quota_exhausted",
            "Today's free Gemini quota is used up; it resets at midnight Pacific time.",
            retries=retries,
            retry_after=seconds_until_quota_reset(now),
        )
    delay = RETRY_DELAY.search(body)
    return AppError(
        429,
        "provider_limited",
        "The AI provider is rate limited.",
        retries=retries,
        retry_after=int(delay.group(1)) if delay else DEFAULT_RETRY_AFTER_SECONDS,
    )


class GeminiClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random: Callable[[], float] = random_module.random,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings
        self.http_client = http_client
        self.sleep = sleep
        self.clock = clock
        self.random = random

    async def generate(
        self,
        model: str,
        system_prompt: str,
        context: dict,
        output_type: type[T],
    ) -> GenerationResult[T]:
        serialized = json.dumps(context, separators=(",", ":"), ensure_ascii=False)
        if len(serialized) > self.settings.ai_context_max_chars:
            raise AppError(
                413,
                "context_too_large",
                "The submitted reasoning is too long to grade.",
            )

        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "PROJECT_DATA_START\n" + serialized + "\nPROJECT_DATA_END"
                    ),
                },
            ],
            "max_completion_tokens": self.settings.ai_max_output_tokens,
            # Reasoning is drawn from max_completion_tokens, so an unbounded
            # thinking budget can consume the response before it is written.
            "reasoning_effort": self.settings.ai_reasoning_effort,
            "tool_choice": "none",
            # Grading should be as close to reproducible as the provider allows:
            # the same answer graded twice should not swing a verdict.
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": output_type.__name__,
                    "strict": True,
                    "schema": output_type.model_json_schema(),
                },
            },
        }

        retries = 0
        while True:
            try:
                response = await self._post(body)
            except NOT_SENT as exc:
                if retries >= self.settings.ai_max_retries:
                    raise self._unavailable(retries) from exc
                await self._retry_delay(retries)
                retries += 1
                continue
            except httpx.TransportError as exc:
                # The request may have reached Gemini and been counted already.
                # Retrying it would spend a second request out of the daily 20.
                raise self._unavailable(retries) from exc

            if response.status_code == 429:
                raise rate_limit_error(response.text, self.clock(), retries)
            if response.status_code >= 500:
                if retries >= self.settings.ai_max_retries:
                    raise self._unavailable(retries)
                await self._retry_delay(retries)
                retries += 1
                continue
            if response.is_error:
                raise AppError(
                    502,
                    "provider_error",
                    "The AI provider rejected the request.",
                    retries=retries,
                )

            try:
                payload = response.json()
                choice = payload["choices"][0]
                if choice.get("finish_reason") == "length":
                    # Reasoning tokens share AI_MAX_OUTPUT_TOKENS with the JSON
                    # itself, so a long deliberation can leave no room to
                    # answer. Distinct from invalid_model_response on purpose:
                    # that code means the evidence rule fired, and a budget
                    # problem wearing it would read as a rubric refusal.
                    raise AppError(
                        502,
                        "response_truncated",
                        "The AI provider's response was cut off before it was complete.",
                        retries=retries,
                    )
                content = choice["message"]["content"]
                value = output_type.model_validate_json(content)
                usage = payload.get("usage") or {}
                return GenerationResult(
                    value=value,
                    prompt_tokens=self._token_count(usage.get("prompt_tokens")),
                    completion_tokens=self._token_count(usage.get("completion_tokens")),
                    retries=retries,
                )
            except (
                KeyError,
                IndexError,
                TypeError,
                ValueError,
                ValidationError,
            ) as exc:
                # A response that fails the evidence rule lands here too: the
                # schema rejects it, so an ungrounded grade is never returned.
                raise AppError(
                    502,
                    "invalid_model_response",
                    "The AI provider returned an invalid response.",
                    retries=retries,
                ) from exc

    async def _post(self, body: dict) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.settings.gemini_api_key}"}
        if self.http_client is not None:
            return await self.http_client.post(
                GEMINI_OPENAI_ENDPOINT,
                headers=headers,
                json=body,
                timeout=self.settings.ai_timeout_seconds,
            )
        async with httpx.AsyncClient() as client:
            return await client.post(
                GEMINI_OPENAI_ENDPOINT,
                headers=headers,
                json=body,
                timeout=self.settings.ai_timeout_seconds,
            )

    async def _retry_delay(self, retries: int) -> None:
        delay = 0.1 * (2**retries) * (0.5 + self.random())
        await self.sleep(delay)

    @staticmethod
    def _token_count(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _unavailable(retries: int) -> AppError:
        return AppError(
            503,
            "provider_unavailable",
            "The AI provider is unavailable.",
            retries=retries,
        )
