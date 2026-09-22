from typing import Literal

from pydantic import Field, computed_field, model_validator
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
    ai_provider: Literal["ollama", "gemini"] = "ollama"
    ai_model_grade: str = ""
    ollama_url: str = "http://localhost:11434"
    ollama_context_length: int = Field(default=8192, ge=2048)
    ollama_think: bool = False
    ollama_keep_alive: int = Field(default=0, ge=0)
    ai_timeout_seconds: float = Field(default=180, gt=0)
    # Gemini only; local generation is never automatically retried.
    ai_max_retries: int = Field(default=1, ge=0)
    ai_context_max_chars: int = Field(default=12_000, gt=0)
    # Local non-thinking output budget. For Gemini, use 8000: its thinking
    # shares this budget and was observed to truncate grades at 2000.
    ai_max_output_tokens: int = Field(default=2048, gt=0)
    # Gemini only. Ollama uses the separate boolean OLLAMA_THINK setting.
    ai_reasoning_effort: Literal["low", "medium", "high"] = "low"
    # Recent grades kept in memory so unchanged reasoning is not re-graded; 0 disables.
    ai_grade_cache_size: int = Field(default=256, ge=0)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def configure_provider(self):
        if not self.ai_model_grade.strip():
            self.ai_model_grade = (
                "grip-grader" if self.ai_provider == "ollama" else "gemini-3.5-flash"
            )
        if (
            self.ai_provider == "gemini"
            and self.ai_model_grade not in STRICT_GEMINI_MODELS
        ):
            supported = ", ".join(sorted(STRICT_GEMINI_MODELS))
            raise ValueError(f"model must support Gemini strict outputs: {supported}")
        if self.ai_provider == "ollama":
            if not self.ollama_url.startswith(("http://", "https://")):
                raise ValueError("OLLAMA_URL must be an HTTP(S) URL")
            if self.ai_max_output_tokens >= self.ollama_context_length:
                raise ValueError(
                    "AI_MAX_OUTPUT_TOKENS must leave room for the prompt in OLLAMA_CONTEXT_LENGTH"
                )
        return self

    @computed_field
    @property
    def cors_origins(self) -> list[str]:
        return [
            value.strip() for value in self.allowed_origins.split(",") if value.strip()
        ]
