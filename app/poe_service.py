"""Poe guide chat. Stateless: nothing stored, message text never logged."""

import asyncio
import logging
from collections import defaultdict, deque
from datetime import timedelta

from app.errors import AppError
from app.ledger import redact
from app.poe_prompt import POE_PROMPT
from app.poe_schemas import PoeChatRequest, PoeReply
from app.service import GradeService

log = logging.getLogger(__name__)

DAY = timedelta(days=1)
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
        self._spent: dict[str, deque] = defaultdict(deque)

    async def chat(
        self, payload: PoeChatRequest, token: str | None, request_id: str
    ) -> PoeReply:
        if not token:
            raise AppError(401, "authentication_required", "Authentication required.")
        session = await self.grading.supabase.validate_session(token)

        settings = self.grading.settings
        if settings.ai_provider != "gemini":
            raise AppError(503, "poe_unavailable", "Poe is resting right now.")
        model = settings.ai_model_poe
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

        spent = self._spend(session.user_id)
        try:
            async with self._slots:
                result = await self.grading.provider.generate(
                    model, POE_PROMPT, self._context(payload), PoeReply
                )
        except AppError as error:
            self._refund(session.user_id, spent)
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

    def _refund(self, user_id: str, spent) -> None:
        try:
            self._spent[user_id].remove(spent)
        except ValueError:
            pass

    def _spend(self, user_id: str):
        """Rolling 24 hour cap per user."""
        now = self.grading.clock()
        spent = self._spent[user_id]
        while spent and now - spent[0] >= DAY:
            spent.popleft()
        if len(spent) >= self.grading.settings.ai_poe_daily_cap:
            wait = int((spent[0] + DAY - now).total_seconds()) + 1 if spent else 3600
            raise AppError(
                429,
                "poe_daily_limit",
                "That's all the questions I can take today. Back tomorrow, friend.",
                retry_after=wait,
            )
        spent.append(now)
        return now
