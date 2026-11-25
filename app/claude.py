"""Claude client for the users in ANTHROPIC_USER_IDS. Same contract as GeminiClient."""

import json
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.errors import AppError
from app.generation import GenerationResult


T = TypeVar("T", bound=BaseModel)

# Model per use: grading needs the strongest judge, Poe only short answers.
CLAUDE_MODELS = {
    "grade": "claude-opus-5-5",
    "general": "claude-sonnet-5-5",
    "poe": "claude-haiku-5-5",
}
EFFORT = {"claude-haiku-5-5": "low"}
MAX_TOKENS = 16_000
DEFAULT_RETRY_AFTER_SECONDS = 60
# Reruns a refused request on another model; Haiku has no server side fallback.
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


def provider_error(exc: anthropic.APIStatusError) -> AppError:
    # Spent credits arrive as 402, a spend cap 429, or a 400 naming the balance.
    if isinstance(
        exc, (anthropic.PaymentRequiredError, anthropic.EnforcedSpendLimitReachedError)
    ) or ("credit balance" in exc.message):
        return AppError(
            503, "provider_credits_exhausted", "The Anthropic credits are used up."
        )
    if isinstance(exc, anthropic.RateLimitError):
        retry = exc.response.headers.get("retry-after", "")
        return AppError(
            429,
            "provider_limited",
            "The AI provider is rate limited.",
            retry_after=int(retry) if retry.isdigit() else DEFAULT_RETRY_AFTER_SECONDS,
        )
    if exc.status_code >= 500:
        return AppError(503, "provider_unavailable", "The AI provider is unavailable.")
    return AppError(502, "provider_error", "The AI provider rejected the request.")


class ClaudeClient:
    def __init__(
        self, settings: Settings, *, client: anthropic.AsyncAnthropic | None = None
    ) -> None:
        self.settings = settings
        self.client = client or anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.ai_timeout_seconds,
            max_retries=settings.ai_max_retries,
        )

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
        try:
            response = await self.client.beta.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": "PROJECT_DATA_START\n"
                        + serialized
                        + "\nPROJECT_DATA_END",
                    }
                ],
                output_config={
                    "effort": EFFORT.get(model, "medium"),
                    "format": {
                        "type": "json_schema",
                        "schema": anthropic.transform_schema(output_type),
                    },
                },
                **({} if model.startswith("claude-haiku") else FALLBACK),
            )
        except anthropic.APIStatusError as exc:
            raise provider_error(exc) from exc
        except anthropic.APIConnectionError as exc:
            raise AppError(
                503, "provider_unavailable", "The AI provider is unavailable."
            ) from exc

        if response.stop_reason == "max_tokens":
            raise AppError(
                502,
                "response_truncated",
                "The AI provider's response was cut off before it was complete.",
            )
        if response.stop_reason == "refusal":
            raise AppError(
                502, "invalid_model_response", "The AI provider declined to answer."
            )
        # Last text block: a declined model's partial text can precede a fallback.
        texts = [block.text for block in response.content if block.type == "text"]
        try:
            value = output_type.model_validate_json(texts[-1] if texts else "")
        except (ValueError, ValidationError) as exc:
            raise AppError(
                502,
                "invalid_model_response",
                "The AI provider returned an invalid response.",
            ) from exc
        return GenerationResult(
            value=value,
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            retries=0,
        )
