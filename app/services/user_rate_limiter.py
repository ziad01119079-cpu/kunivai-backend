"""Single-worker authenticated-user rate limiting for the chat endpoint."""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque


class UserRateLimitExceeded(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


class UserRateLimiter:
    def __init__(self, *, requests: int, window_seconds: int):
        if requests < 1 or window_seconds < 1:
            raise ValueError("invalid user rate limit")
        self.requests = requests
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, uid: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        async with self._lock:
            events = self._events[uid]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.requests:
                retry_after = max(1, int(events[0] + self.window_seconds - current))
                raise UserRateLimitExceeded(retry_after)
            events.append(current)
            if not events:
                self._events.pop(uid, None)


chat_user_rate_limiter = UserRateLimiter(
    requests=int(os.getenv("AI_USER_RATE_LIMIT_REQUESTS", "20")),
    window_seconds=int(os.getenv("AI_USER_RATE_LIMIT_WINDOW_SECONDS", "60")),
)
