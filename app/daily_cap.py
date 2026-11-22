"""Rolling 24 hour request cap per user, shared by every model-calling feature."""

from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timedelta

from app.errors import AppError

DAY = timedelta(days=1)


class DailyCap:
    """ponytail: process memory, so a restart or Render wake resets it; accepted
    risk (code scanning #8). Move to Supabase if that starts costing quota."""

    def __init__(
        self, cap: int, code: str, message: str, clock: Callable[[], datetime]
    ) -> None:
        self.cap = cap
        self.code = code
        self.message = message
        self.clock = clock
        self._spent: dict[str, deque] = defaultdict(deque)

    def spend(self, user_id: str) -> datetime:
        """Count one call, or raise 429 with the seconds until a slot frees up."""
        now = self.clock()
        spent = self._spent[user_id]
        while spent and now - spent[0] >= DAY:
            spent.popleft()
        if len(spent) >= self.cap:
            wait = int((spent[0] + DAY - now).total_seconds()) + 1 if spent else 3600
            raise AppError(429, self.code, self.message, retry_after=wait)
        spent.append(now)
        return now

    def refund(self, user_id: str, spent: datetime) -> None:
        """Give back a call the provider never answered."""
        try:
            self._spent[user_id].remove(spent)
        except ValueError:
            pass
