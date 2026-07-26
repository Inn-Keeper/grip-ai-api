"""Gemini client, adapted from ativscrum-ai-api.

Unchanged in substance: strict JSON schema output, retry with jitter on
transport and 5xx only, and a typed error for every distinct failure so the
caller can tell "rate limited" from "returned nonsense".
"""

import asyncio
import json
import random as random_module
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.errors import AppError


T = TypeVar("T", bound=BaseModel)

GEMINI_OPENAI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)


@dataclass(frozen=True)
class GeminiResult(Generic[T]):
    value: T
    prompt_tokens: int | None
    completion_tokens: int | None
    retries: int


class GeminiClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random: Callable[[], float] = random_module.random,
    ) -> None:
        self.settings = settings
        self.http_client = http_client
        self.sleep = sleep
        self.random = random

    async def generate(
        self,
        model: str,
        system_prompt: str,
        context: dict,
        output_type: type[T],
    ) -> GeminiResult[T]:
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
            except httpx.TransportError as exc:
                if retries >= self.settings.ai_max_retries:
                    raise self._unavailable(retries) from exc
                await self._retry_delay(retries)
                retries += 1
                continue

            if response.status_code == 429:
                raise AppError(
                    429,
                    "provider_limited",
                    "The AI provider is rate limited.",
                    retries=retries,
                )
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
                content = payload["choices"][0]["message"]["content"]
                value = output_type.model_validate_json(content)
                usage = payload.get("usage") or {}
                return GeminiResult(
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
