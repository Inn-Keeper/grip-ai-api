"""Grading orchestration.

Nothing here is persisted, matching ativscrum-ai-api. The caller sends the
board's reasoning plus the facts derived from it, and gets a grade back to store
itself through its own Supabase session. Recent grades are only cached in
memory, so resubmitting unchanged reasoning costs no provider quota.
"""

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.config import Settings
from app.errors import AppError
from app.gemini import GeminiClient
from app.ollama import OllamaClient
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

# A section this short cannot answer its question: "REST", "the db".
# ponytail: word count is a blunt proxy for "placeholder" — a terse real answer
# of four words scores missing too. Replace with a smarter check if that bites.
PLACEHOLDER_MAX_WORDS = 4


def validate_evidence(suggestion: GradeSuggestion, sections: dict) -> None:
    """Reject model quotations that are absent from the candidate's section."""
    for grade in suggestion.sections:
        evidence = grade.evidence.strip()
        if evidence and evidence not in sections[grade.section]:
            raise AppError(
                502,
                "invalid_model_response",
                "The model quoted evidence that was not in the submitted reasoning.",
            )


def apply_placeholder_floor(
    suggestion: GradeSuggestion, sections: dict
) -> GradeSuggestion:
    """Grade near-empty sections "missing", whatever the model said.

    The rubric already calls placeholders missing, but the local model still
    rated them "thin" (half marks). A prompt can be ignored; a word count cannot.
    """
    graded = [
        item.model_copy(update={"verdict": "missing", "evidence": ""})
        if len(sections[item.section].split()) <= PLACEHOLDER_MAX_WORDS
        else item
        for item in suggestion.sections
    ]
    return suggestion.model_copy(update={"sections": graded})


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


class GradeCache:
    """The most recent successful grades, keyed by everything the model sees.

    ponytail: in-process LRU, lost on restart and not shared between workers;
    persist through arch_boards.talk_grade once the client integration lands.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self._grades: OrderedDict[str, GradeSuggestion] = OrderedDict()

    def get(self, key: str) -> GradeSuggestion | None:
        grade = self._grades.get(key)
        if grade is not None:
            self._grades.move_to_end(key)
        return grade

    def put(self, key: str, grade: GradeSuggestion) -> None:
        self._grades[key] = grade
        self._grades.move_to_end(key)
        if len(self._grades) > self.size:
            self._grades.popitem(last=False)


def grade_cache_key(user_id: str, model: str, context: dict) -> str:
    # The rubric is part of the key, so a prompt change regrades everything.
    material = json.dumps([user_id, model, SYSTEM_PROMPT, context], sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()


class GradeService:
    """Grades a talk track, and remembers when the provider said to come back.

    Google offers no way to ask how much free-tier quota is left, so the 429 is
    the only signal there is. Holding on to it turns one wasted request into
    every later attempt being answered here, for nothing.

    ponytail: in-process, like the grade cache. A second worker learns the
    limit separately, which costs one request each.
    """

    def __init__(
        self,
        settings: Settings,
        supabase: SupabaseGateway,
        provider: GeminiClient | OllamaClient,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings
        self.supabase = supabase
        self.provider = provider
        self.clock = clock
        self.cache = GradeCache(settings.ai_grade_cache_size)
        self._limit: AppError | None = None
        self._limited_until: datetime | None = None

    def _current_limit(self) -> AppError | None:
        """The live limit with its countdown refreshed, or None once it lapses."""
        if self._limit is None or self._limited_until is None:
            return None
        remaining = int((self._limited_until - self.clock()).total_seconds())
        if remaining <= 0:
            self._limit = self._limited_until = None
            return None
        return AppError(
            self._limit.status_code,
            self._limit.code,
            self._limit.message,
            retry_after=remaining,
        )

    def _remember(self, error: AppError) -> None:
        if error.status_code == 429 and error.retry_after:
            self._limit = error
            self._limited_until = self.clock() + timedelta(seconds=error.retry_after)

    def grading_status(self) -> dict:
        """Whether grading would go through, answered without asking Google."""
        limit = self._current_limit()
        if limit is None:
            return {"grading": "available"}
        return {
            "grading": "unavailable",
            "code": limit.code,
            "message": limit.message,
            "retry_after": limit.retry_after,
        }

    async def grade(
        self,
        payload: GradeRequest,
        token: str | None,
        request_id: str,
    ) -> GradeResponse:
        if not token:
            raise AppError(401, "authentication_required", "Authentication required.")
        session = await self.supabase.validate_session(token)

        limit = self._current_limit()
        if limit is not None:
            raise limit

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

        model = self.settings.ai_model_grade
        context = build_context(
            payload.facts.model_dump(), sections, payload.self_rating
        )
        key = grade_cache_key(session.user_id, model, context)
        suggestion = self.cache.get(key)
        if suggestion is None:
            try:
                result = await self.provider.generate(
                    model, SYSTEM_PROMPT, context, GradeSuggestion
                )
            except AppError as error:
                self._remember(error)
                raise
            validate_evidence(result.value, sections)
            suggestion = apply_placeholder_floor(result.value, sections)
            self.cache.put(key, suggestion)
        score = score_from_verdicts(suggestion)
        return GradeResponse(
            request_id=UUID(request_id),
            model=model,
            board_id=payload.board_id,
            score=score,
            divergence=divergence_from(payload.self_rating, score),
            suggestion=suggestion,
        )
