"""Poe guide chat. Stateless: nothing stored, message text never logged."""

import asyncio
import logging
from app.daily_cap import DailyCap
from app.errors import AppError
from app.ledger import redact
from app.poe_prompt import POE_PROMPT
from app.poe_schemas import PoeChatRequest, PoeReply
from app.service import GradeService

log = logging.getLogger(__name__)

BUSY_RETRY_SECONDS = 30
MODEL_TURNS = 10


class PoeChatService:
    """Same shape as PlaybookService, on its own model, slots and cap.

    ponytail: cap and slots are per process memory; move counters to Supabase
    if the API runs on more than one instance.
    """

    def __init__(self, grading: GradeService) -> None:
        self.grading = grading
        self._slots = asyncio.Semaphore(grading.settings.ai_poe_max_concurrent)
        self.cap = DailyCap(
            grading.settings.ai_poe_daily_cap,
            "poe_daily_limit",
            "That's all the questions I can take today. Back tomorrow, friend.",
            lambda: grading.clock(),
        )

    async def chat(
        self, payload: PoeChatRequest, token: str | None, request_id: str
    ) -> PoeReply:
        if not token:
            raise AppError(401, "authentication_required", "Authentication required.")
        session = await self.grading.supabase.validate_session(token)

        settings = self.grading.settings
        model = self.grading.model_for(session.user_id, "poe")
        if settings.ai_provider != "gemini" and not model.startswith("claude-"):
            raise AppError(503, "poe_unavailable", "Poe is resting right now.")
        limit = self.grading.current_limit(model)
        if limit is not None:
            raise limit
        if self._slots.locked():
            raise AppError(
                503,
                "busy",
                "Poe is helping someone else. Try again shortly.",
                retry_after=BUSY_RETRY_SECONDS,
            )

        spent = self.cap.spend(session.user_id)
        try:
            async with self._slots:
                result = await self.grading.provider_for(model).generate(
                    model, POE_PROMPT, self._context(payload), PoeReply
                )
        except AppError as error:
            self.cap.refund(session.user_id, spent, error)
            self.grading.remember(error, model)
            log.info("poe %s: %s", request_id, error.code)
            raise
        log.info("poe %s: ok", request_id)
        return result.value

    @staticmethod
    def _context(payload: PoeChatRequest) -> dict:
        conversation = [
            {
                "role": turn.role,
                "text": redact(turn.text)[0] if turn.role == "user" else turn.text,
            }
            for turn in payload.messages[-MODEL_TURNS:]
        ]
        return {"locale": payload.locale, "conversation": conversation}
