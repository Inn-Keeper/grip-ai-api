"""Reads posting links for import review. Logs counts per status only."""

import asyncio
import logging
from collections import Counter, defaultdict, deque
from datetime import timedelta

from app.errors import AppError
from app.import_schemas import PostingResult, PostingsRequest, PostingsResponse
from app.posting_reader import PostingReader
from app.service import GradeService

log = logging.getLogger(__name__)

WINDOW = timedelta(minutes=10)
URLS_PER_WINDOW = 30
CONCURRENCY = 3
REQUEST_SECONDS = 20


class PostingService:
    def __init__(
        self, grading: GradeService, reader: PostingReader | None = None
    ) -> None:
        self.grading = grading
        self.reader = reader or PostingReader()
        # ponytail: per process memory, like the quota memory; shared store if the API scales out.
        self._spent: dict[str, deque] = defaultdict(deque)

    async def read(
        self, payload: PostingsRequest, token: str | None, request_id: str
    ) -> PostingsResponse:
        if not token:
            raise AppError(401, "authentication_required", "Authentication required.")
        session = await self.grading.supabase.validate_session(token)
        urls = list(dict.fromkeys(payload.urls))
        self._spend(session.user_id, len(urls))

        gate = asyncio.Semaphore(CONCURRENCY)

        async def one(url: str) -> PostingResult:
            async with gate:
                posting = await self.reader.read(url)
            return PostingResult(url=url, status=posting.status, text=posting.text)

        tasks = [asyncio.ensure_future(one(url)) for url in urls]
        done, pending = await asyncio.wait(tasks, timeout=REQUEST_SECONDS)
        for task in pending:
            task.cancel()
        results = [
            task.result()
            if task in done
            else PostingResult(url=url, status="unreachable", text=None)
            for url, task in zip(urls, tasks)
        ]
        log.info(
            "postings %s: %s", request_id, dict(Counter(r.status for r in results))
        )
        return PostingsResponse(postings=results)

    def _spend(self, user_id: str, count: int) -> None:
        now = self.grading.clock()
        spent = self._spent[user_id]
        while spent and now - spent[0] >= WINDOW:
            spent.popleft()
        if len(spent) + count > URLS_PER_WINDOW:
            wait = int((spent[0] + WINDOW - now).total_seconds()) + 1
            raise AppError(
                429,
                "rate_limited",
                "Too many links read. Try again in a few minutes.",
                retry_after=wait,
            )
        spent.extend([now] * count)
