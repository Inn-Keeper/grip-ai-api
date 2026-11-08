"""First-run playbook generation. Nothing is stored or logged except counts;
the web app saves the stories and scenario itself."""

import asyncio
import hashlib
import json
import logging
from collections import defaultdict, deque
from datetime import timedelta

from app.errors import AppError
from app.playbook_prompt import PLAYBOOK_PROMPT
from app.playbook_schemas import ModelPlaybook, PlaybookRequest, validate_playbook
from app.service import GradeCache, GradeService

log = logging.getLogger(__name__)

DAY = timedelta(days=1)
BUSY_RETRY_SECONDS = 30
CACHE_SIZE = 128


class PlaybookService:
    """Shares the grading service's provider, auth and quota memory.

    ponytail: cap, slots and cache are per process memory; move counters to
    Supabase if the API runs on more than one instance.
    """

    def __init__(self, grading: GradeService) -> None:
        self.grading = grading
        self.cache = GradeCache(CACHE_SIZE)
        self._slots = asyncio.Semaphore(grading.settings.ai_max_concurrent)
        self._spent: dict[str, deque] = defaultdict(deque)

    async def generate(
        self, payload: PlaybookRequest, token: str | None, request_id: str
    ) -> ModelPlaybook:
        if not token:
            raise AppError(401, "authentication_required", "Authentication required.")
        session = await self.grading.supabase.validate_session(token)

        limit = self.grading.current_limit()
        if limit is not None:
            raise limit

        model = self.grading.settings.ai_model_grade
        key = self._key(session.user_id, model, payload)
        cached = self.cache.get(key)
        if cached is not None:
            log.info("playbook %s: cache hit", request_id)
            return cached

        # Busy is checked before the cap so a refused request costs the user nothing.
        if self._slots.locked():
            raise AppError(
                503,
                "busy",
                "Grip is busy writing other battle plans. Try again shortly.",
                retry_after=BUSY_RETRY_SECONDS,
            )
        spent = self._spend(session.user_id)
        try:
            async with self._slots:
                try:
                    result = await self.grading.provider.generate(
                        model, PLAYBOOK_PROMPT, payload.model_dump(), ModelPlaybook
                    )
                except AppError as error:
                    self.grading.remember(error)
                    raise
            playbook = result.value
            validate_playbook(playbook, payload.competencies, payload.node_types)
        except AppError as error:
            # Only playbooks the user received count toward the daily cap.
            self._refund(session.user_id, spent)
            log.info("playbook %s: %s", request_id, error.code)
            raise
        self.cache.put(key, playbook)
        log.info(
            "playbook %s: %d stories, %d checks",
            request_id,
            len(playbook.stories),
            len(playbook.scenario.checks),
        )
        return playbook

    @staticmethod
    def _key(user_id: str, model: str, payload: PlaybookRequest) -> str:
        material = json.dumps(
            [user_id, model, PLAYBOOK_PROMPT, payload.model_dump()], sort_keys=True
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def _refund(self, user_id: str, spent) -> None:
        try:
            self._spent[user_id].remove(spent)
        except ValueError:
            pass

    def _spend(self, user_id: str):
        """Rolling 24 hour cap per user; cache hits never reach here."""
        now = self.grading.clock()
        spent = self._spent[user_id]
        while spent and now - spent[0] >= DAY:
            spent.popleft()
        if len(spent) >= self.grading.settings.ai_playbook_daily_cap:
            wait = int((spent[0] + DAY - now).total_seconds()) + 1 if spent else 3600
            raise AppError(
                429,
                "playbook_daily_limit",
                "You've generated today's battle plans. Try again tomorrow.",
                retry_after=wait,
            )
        spent.append(now)
        return now
