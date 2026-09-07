"""Native Ollama chat API with schema-constrained, non-streaming output."""

import asyncio
import json
from typing import TypeVar

import httpx
from pydantic import BaseModel

from app.config import Settings
from app.errors import AppError
from app.generation import GenerationResult


T = TypeVar("T", bound=BaseModel)


class OllamaClient:
    def __init__(
        self, settings: Settings, *, http_client: httpx.AsyncClient | None = None
    ):
        self.settings = settings
        self.http_client = http_client
        # One generation at a time per process on the 16 GB development Mac.
        self._lock = asyncio.Lock()

    async def generate(
        self, model: str, system_prompt: str, context: dict, output_type: type[T]
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
            "stream": False,
            "think": self.settings.ollama_think,
            "keep_alive": self.settings.ollama_keep_alive,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": "PROJECT_DATA_START\n"
                    + serialized
                    + "\nPROJECT_DATA_END",
                },
            ],
            "format": output_type.model_json_schema(),
            "options": {
                "num_ctx": self.settings.ollama_context_length,
                "num_predict": self.settings.ai_max_output_tokens,
                "temperature": 0,
            },
        }
        # Do not retry: a timed-out local generation may still be running.
        try:
            async with self._lock:
                response = await self._post(body)
        except httpx.TransportError as exc:
            raise AppError(
                503,
                "provider_unavailable",
                "Ollama is unavailable or timed out. Check that it is running.",
            ) from exc

        if response.status_code == 404:
            raise AppError(
                503,
                "model_not_found",
                "Ollama could not find the configured model. Pull or create it first.",
            )
        if response.status_code == 429:
            raise AppError(429, "provider_limited", "Ollama is busy. Try again later.")
        if response.status_code >= 500:
            raise AppError(
                503, "provider_unavailable", "Ollama could not complete the request."
            )
        if response.is_error:
            raise AppError(502, "provider_error", "Ollama rejected the request.")

        try:
            payload = response.json()
            if payload.get("done_reason") == "length" or payload.get("done") is False:
                raise AppError(
                    502,
                    "response_truncated",
                    "Ollama's response was cut off before it was complete.",
                )
            if payload.get("done") is not True:
                raise ValueError("Missing completion marker")
            value = output_type.model_validate_json(payload["message"]["content"])
            return GenerationResult(
                value=value,
                prompt_tokens=self._token_count(payload.get("prompt_eval_count")),
                completion_tokens=self._token_count(payload.get("eval_count")),
                retries=0,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise AppError(
                502, "invalid_model_response", "Ollama returned an invalid response."
            ) from exc

    async def _post(self, body: dict) -> httpx.Response:
        url = f"{self.settings.ollama_url.rstrip('/')}/api/chat"
        if self.http_client is not None:
            return await self.http_client.post(
                url, json=body, timeout=self.settings.ai_timeout_seconds
            )
        async with httpx.AsyncClient(
            timeout=self.settings.ai_timeout_seconds
        ) as client:
            return await client.post(url, json=body)

    @staticmethod
    def _token_count(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None
