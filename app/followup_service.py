"""Grades answers to interviewer follow-ups. Stores nothing."""

import hashlib
import json
from uuid import UUID

from app.errors import AppError
from app.followup_prompt import FOLLOWUP_PROMPT
from app.followup_schemas import (
    AnswerGrade,
    FollowupGradeRequest,
    FollowupGradeResponse,
    FollowupSuggestion,
)
from app.prompts import build_context
from app.service import (
    GradeCache,
    GradeService,
    apply_placeholder_floor,
    score_from_verdicts,
    validate_evidence,
)


class FollowupGradeService:
    """Shares the grading service's provider, auth and quota memory."""

    def __init__(self, grading: GradeService) -> None:
        self.grading = grading
        self.cache = GradeCache(grading.settings.ai_grade_cache_size)

    async def grade(
        self, payload: FollowupGradeRequest, token: str | None, request_id: str
    ) -> FollowupGradeResponse:
        if not token:
            raise AppError(401, "authentication_required", "Authentication required.")
        session = await self.grading.supabase.validate_session(token)

        ids = [f"q{n}" for n in range(1, len(payload.followups) + 1)]
        questions = dict(zip(ids, (item.question for item in payload.followups)))
        # Only answered questions reach the model; blanks are missing by definition.
        answers = {
            key: item.answer
            for key, item in zip(ids, payload.followups)
            if item.answer.strip()
        }
        if not answers:
            raise AppError(
                422, "nothing_to_grade", "Answer a follow-up before asking for a grade."
            )

        settings = self.grading.settings
        model = settings.ai_model_grade
        context = build_context(payload.facts.model_dump(), {}, None)
        del context["candidate_sections"]
        context["followup_answers"] = [
            {"id": key, "question": questions[key], "answer": answer}
            for key, answer in answers.items()
        ]
        serialized = json.dumps(context, separators=(",", ":"), ensure_ascii=False)
        if len(serialized) > settings.ai_context_max_chars:
            raise AppError(
                413, "context_too_large", "The answers are too long to grade."
            )

        # The rubric is part of the key, so a prompt change regrades everything.
        key = hashlib.sha256(
            json.dumps(
                [session.user_id, model, FOLLOWUP_PROMPT, context], sort_keys=True
            ).encode()
        ).hexdigest()
        suggestion = self.cache.get(key)
        if suggestion is None:
            limit = self.grading.current_limit()
            if limit is not None:
                raise limit
            try:
                result = await self.grading.provider.generate(
                    model, FOLLOWUP_PROMPT, context, FollowupSuggestion
                )
            except AppError as error:
                self.grading.remember(error)
                raise
            if sorted(item.section for item in result.value.sections) != sorted(
                answers
            ):
                raise AppError(
                    502,
                    "invalid_model_response",
                    "The model did not grade every answer exactly once.",
                )
            validate_evidence(result.value, answers)
            suggestion = apply_placeholder_floor(result.value, answers)
            self.cache.put(key, suggestion)

        graded = {item.section: item for item in suggestion.sections}
        # An unanswered question is still the next thing to ask.
        full = [
            graded.get(key)
            or AnswerGrade(section=key, verdict="missing", gap=questions[key])
            for key in ids
        ]
        return FollowupGradeResponse(
            request_id=UUID(request_id),
            model=model,
            board_id=payload.board_id,
            score=score_from_verdicts(FollowupSuggestion(sections=full)),
            answers=full,
        )
