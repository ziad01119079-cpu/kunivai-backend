"""Coalesce concurrent retries of the same logical streaming chat request."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from .community_replay_guard import (
    extract_community_refs_from_payload,
    filter_replay_sequence,
)


@dataclass
class _RequestState:
    events: list[str] = field(default_factory=list)
    subscribers: set[asyncio.Queue[str | None]] = field(default_factory=set)
    completed_at: float | None = None
    replay_bytes: int = 0
    replayable: bool = True
    community_ids: set[str] = field(default_factory=set)
    community_revoked: bool = False


class RequestCoalescer:
    def __init__(
        self,
        *,
        replay_ttl_seconds: float = 120.0,
        max_entries: int = 200,
        max_replay_bytes: int = 262_144,
    ):
        self._replay_ttl_seconds = replay_ttl_seconds
        self._max_entries = max_entries
        self._max_replay_bytes = max_replay_bytes
        self._states: OrderedDict[str, _RequestState] = OrderedDict()
        self._guard = asyncio.Lock()

    async def stream(
        self,
        key: str,
        factory: Callable[[], AsyncIterator[str]],
    ) -> AsyncIterator[str]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        now = time.monotonic()
        replay_snapshot: tuple[str, ...] | None = None
        replay_completed = False
        async with self._guard:
            self._prune(now)
            state = self._states.get(key)
            if state is None:
                state = _RequestState()
                self._states[key] = state
                state.subscribers.add(queue)
                asyncio.create_task(self._pump(key, state, factory))
            elif not state.replayable:
                queue.put_nowait(self._replay_unavailable_event())
                queue.put_nowait(None)
            elif state.completed_at is not None:
                replay_snapshot = tuple(state.events)
                replay_completed = True
                self._states.move_to_end(key)
            else:
                replay_snapshot = tuple(state.events)
                state.subscribers.add(queue)

        if replay_snapshot is not None:
            safe_replay = await filter_replay_sequence(replay_snapshot)
            for event in safe_replay:
                queue.put_nowait(event)
            if replay_completed:
                queue.put_nowait(None)

        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            async with self._guard:
                state = self._states.get(key)
                if state is not None:
                    state.subscribers.discard(queue)

    async def _pump(
        self,
        key: str,
        state: _RequestState,
        factory: Callable[[], AsyncIterator[str]],
    ) -> None:
        try:
            async for event in factory():
                encoded_size = len(event.encode("utf-8"))
                # Track referenced Community IDs for proactive retraction invalidation (COMM-004).
                if event.startswith("data:"):
                    try:
                        parsed = json.loads(event[5:].strip())
                        if isinstance(parsed, dict):
                            _, refs = extract_community_refs_from_payload(parsed)
                            for ref in refs:
                                state.community_ids.add(ref["community_id"])
                    except ValueError:
                        pass
                if state.replayable and state.replay_bytes + encoded_size <= self._max_replay_bytes:
                    state.events.append(event)
                    state.replay_bytes += encoded_size
                else:
                    state.replayable = False
                    state.events.clear()
                    state.replay_bytes = 0
                for subscriber in tuple(state.subscribers):
                    subscriber.put_nowait(event)
        finally:
            state.completed_at = time.monotonic()
            for subscriber in tuple(state.subscribers):
                subscriber.put_nowait(None)
            async with self._guard:
                if self._states.get(key) is state:
                    self._states.move_to_end(key)
                    self._prune(state.completed_at)

    def invalidate_community_replay(self, community_id: str) -> int:
        """Mark or evict completed local replay states referencing a retracted Community record."""
        affected = 0
        for state in self._states.values():
            if community_id in state.community_ids:
                state.community_revoked = True
                affected += 1
        return affected

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, state in self._states.items()
            if state.completed_at is not None
            and now - state.completed_at >= self._replay_ttl_seconds
        ]
        for key in expired:
            self._states.pop(key, None)
        while len(self._states) > self._max_entries:
            key, state = next(iter(self._states.items()))
            if state.completed_at is None:
                break
            self._states.pop(key, None)

    @staticmethod
    def _replay_unavailable_event() -> str:
        return "data: " + json.dumps({
            "type": "error",
            "code": "REPLAY_BUFFER_LIMIT",
            "retryable": True,
            "message": "تعذر إعادة بث هذه الإجابة بأمان. أرسل السؤال مرة أخرى.",
        }) + "\n\n"


chat_request_coalescer = RequestCoalescer(
    replay_ttl_seconds=float(os.getenv("AI_REPLAY_TTL_SECONDS", "120")),
    max_entries=int(os.getenv("AI_REPLAY_MAX_ENTRIES", "200")),
    max_replay_bytes=int(os.getenv("AI_REPLAY_MAX_BYTES", "262144")),
)
