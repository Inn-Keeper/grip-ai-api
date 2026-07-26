from typing import Literal

from pydantic import computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Copied from ativscrum-ai-api. Grading depends on strict JSON output, so a
# model that does not support it must be rejected at startup rather than
# discovered at runtime. Confirm these ids against the provider's current model
# list before deploying.
STRICT_GEMINI_MODELS = frozenset({"gemini-3.1-flash-lite", "gemini-3.5-flash"})


class Settings(BaseSettings):
    app_env: str = "development"
    allowed_origins: str = "http://localhost:5173"
    supabase_url: str = ""
    supabase_anon_key: str = ""
    gemini_api_key: str = ""
    # Grading is judgement work, not extraction: default to the stronger model.
    ai_model_grade: str = "gemini-3.5-flash"
    ai_timeout_seconds: float = 30
    ai_max_retries: int = 1
    ai_context_max_chars: int = 24_000
    # Shared with the model's reasoning tokens, not just the JSON it returns.
    # Grading a talk track was measured at up to ~1,900 reasoning tokens before
    # ~650 tokens of answer, so 2,000 truncated most responses.
    ai_max_output_tokens: int = 8_000
    # The thinking budget is dynamic and grows into whatever room it is given,
    # so the cap above is not on its own enough to keep the JSON from being cut
    # off. This is a calibration knob, not a constant: raise it if the pushover
    # fixtures start passing, which would mean the grader has gone lenient.
    ai_reasoning_effort: Literal["low", "medium", "high"] = "low"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("ai_model_grade")
    @classmethod
    def require_strict_gemini_model(cls, value: str) -> str:
        if value not in STRICT_GEMINI_MODELS:
            supported = ", ".join(sorted(STRICT_GEMINI_MODELS))
            raise ValueError(f"model must support Gemini strict outputs: {supported}")
        return value

    @computed_field
    @property
    def cors_origins(self) -> list[str]:
        return [
            value.strip() for value in self.allowed_origins.split(",") if value.strip()
        ]
