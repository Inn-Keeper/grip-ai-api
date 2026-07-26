"""Grading orchestration.

Stateless by design, matching ativscrum-ai-api: nothing here is persisted. The
caller sends the board's reasoning plus the facts derived from it, and gets a
grade back to store itself through its own Supabase session.
"""

from uuid import UUID

from app.config import Settings
from app.errors import AppError
from app.gemini import GeminiClient
from app.prompts import SYSTEM_PROMPT, build_context
from app.schemas import (
    SECTION_IDS,
    VERDICT_POINTS,
    GradeRequest,
    GradeResponse,
    GradeSuggestion,
)
from app.supabase import SupabaseGateway


# The self-rating is 1-5; grades are percentages.
SELF_RATING_MAX = 5


def score_from_verdicts(suggestion: GradeSuggestion) -> int:
    """Derive the score from per-section verdicts.

    Deliberately not the model's job. Asking a model for a number invites a
    generous one; asking it to classify six sections and doing the arithmetic
    here means leniency has to show up somewhere specific and testable.
    """
    points = [VERDICT_POINTS[item.verdict] for item in suggestion.sections]
    return round(sum(points) / len(points))


def divergence_from(self_rating: int | None, score: int) -> int | None:
    """How much the candidate over-rated themselves, in percentage points.

    Positive means they were more confident than the grade supports, which is
    the number worth showing before an interview. Negative (under-confidence)
    is reported too rather than clamped — it is also worth knowing.
    """
    if self_rating is None:
        return None
    return round((self_rating / SELF_RATING_MAX) * 100) - score


class GradeService:
    def __init__(
        self,
        settings: Settings,
        supabase: SupabaseGateway,
        gemini: GeminiClient,
    ) -> None:
        self.settings = settings
        self.supabase = supabase
        self.gemini = gemini

    async def grade(
        self,
        payload: GradeRequest,
        token: str | None,
        request_id: str,
    ) -> GradeResponse:
        if not token:
            raise AppError(401, "authentication_required", "Authentication required.")
        await self.supabase.validate_session(token)

        sections = {
            section_id: payload.sections.get(section_id, "")
            for section_id in SECTION_IDS
        }
        if not any(text.strip() for text in sections.values()):
            raise AppError(
                422,
                "nothing_to_grade",
                "Write some reasoning before asking for a grade.",
            )

        result = await self.gemini.generate(
            self.settings.ai_model_grade,
            SYSTEM_PROMPT,
            build_context(payload.facts.model_dump(), sections, payload.self_rating),
            GradeSuggestion,
        )

        score = score_from_verdicts(result.value)
        return GradeResponse(
            request_id=UUID(request_id),
            model=self.settings.ai_model_grade,
            board_id=payload.board_id,
            score=score,
            divergence=divergence_from(payload.self_rating, score),
            suggestion=result.value,
        )
