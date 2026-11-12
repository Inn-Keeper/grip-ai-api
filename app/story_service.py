"""STAR story grading. Stores nothing; the client saves the grade itself."""

import hashlib
import json
from uuid import UUID

from app.errors import AppError
from app.service import (
    GradeCache,
    GradeService,
    apply_placeholder_floor,
    score_from_verdicts,
    validate_evidence,
)
from app.story_prompt import STORY_PROMPT
from app.story_schemas import (
    STORY_SECTION_IDS,
    StoryGradeRequest,
    StoryGradeResponse,
    StoryGradeSuggestion,
)


class StoryGradeService:
    """Shares the grading service's provider, auth and quota memory."""

    def __init__(self, grading: GradeService) -> None:
        self.grading = grading
        self.cache = GradeCache(grading.settings.ai_grade_cache_size)

    async def grade(
        self, payload: StoryGradeRequest, token: str | None, request_id: str
    ) -> StoryGradeResponse:
        if not token:
            raise AppError(401, "authentication_required", "Authentication required.")
        session = await self.grading.supabase.validate_session(token)

        sections = {
            section_id: payload.sections.get(section_id, "")
            for section_id in STORY_SECTION_IDS
        }
        if not any(text.strip() for text in sections.values()):
            raise AppError(
                422, "nothing_to_grade", "Write the story before asking for a grade."
            )

        settings = self.grading.settings
        model = settings.ai_model_grade
        context = {
            "competency": payload.competency,
            "title": payload.title,
            "candidate_sections": sections,
        }
        serialized = json.dumps(context, separators=(",", ":"), ensure_ascii=False)
        if len(serialized) > settings.ai_context_max_chars:
            raise AppError(413, "context_too_large", "The story is too long to grade.")

        # The rubric is part of the key, so a prompt change regrades everything.
        key = hashlib.sha256(
            json.dumps(
                [session.user_id, model, STORY_PROMPT, context], sort_keys=True
            ).encode()
        ).hexdigest()
        suggestion = self.cache.get(key)
        if suggestion is None:
            limit = self.grading.current_limit()
            if limit is not None:
                raise limit
            try:
                result = await self.grading.provider.generate(
                    model, STORY_PROMPT, context, StoryGradeSuggestion
                )
            except AppError as error:
                self.grading.remember(error)
                raise
            validate_evidence(result.value, sections)
            suggestion = apply_placeholder_floor(result.value, sections)
            self.cache.put(key, suggestion)
        return StoryGradeResponse(
            request_id=UUID(request_id),
            model=model,
            story_id=payload.story_id,
            score=score_from_verdicts(suggestion),
            suggestion=suggestion,
        )
