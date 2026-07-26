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
    ai_max_output_tokens: int = 2_000

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
