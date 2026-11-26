"""Coalesce concurrent retries of the same logical streaming chat request (H2 hardened).

Uses a single sequenced event log (`state.events`) + `asyncio.Condition` with a
per-subscriber integer cursor and bounded lag limit:
- Zero catch-up vs. live reordering race (every subscriber reads strictly in sequence).
- Every Community event is revalidated via `filter_sse_event_for_replay` immediately
  before delivery (both on replay AND on live dequeue).
- Bounded subscriber lag (`max_lag_events`) and clean detachment on cancellation.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from .community_replay_guard import (
    _split_sse_event,
    community_revoked_replay_error,
    extract_community_refs_from_payload,
    filter_sse_event_for_replay,
)


@dataclass
class _RequestState:
    events: list[str] = field(default_factory=list)
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    overflow_queues: set[asyncio.Queue[str | None]] = field(default_factory=set)
    active_subscribers: int = 0
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
        max_lag_events: int = 512,
    ):
        self._replay_ttl_seconds = replay_ttl_seconds
        self._max_entries = max_entries
        self._max_replay_bytes = max_replay_bytes
        self._max_lag_events = max_lag_events
        self._states: OrderedDict[str, _RequestState] = OrderedDict()
        self._guard = asyncio.Lock()

    async def stream(
        self,
        key: str,
        factory: Callable[[], AsyncIterator[str]],
    ) -> AsyncIterator[str]:
        now = time.monotonic()
        overflow_q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self._max_lag_events)
        async with self._guard:
            self._prune(now)
            state = self._states.get(key)
            if state is None:
                state = _RequestState()
                self._states[key] = state
                state.active_subscribers += 1
                state.overflow_queues.add(overflow_q)
                asyncio.create_task(self._pump(key, state, factory))
            elif not state.replayable:
                yield self._replay_unavailable_event()
                return
            else:
                state.active_subscribers += 1
                state.overflow_queues.add(overflow_q)
                self._states.move_to_end(key)

        cursor = 0
        had_non_community_chunk = False
        had_any_surviving_chunk = False
        any_revoked = False

        try:
            while True:
                async with state.cond:
                    while cursor >= len(state.events) and state.completed_at is None and state.replayable:
                        await state.cond.wait()

                    if not state.replayable:
                        # Replay buffer exceeded: seamlessly drain bounded live overflow queue
                        # without interrupting already-attached subscribers.
                        raw_event = None
                    else:
                        lag = len(state.events) - cursor
                        if lag > self._max_lag_events:
                            yield self._replay_unavailable_event()
                            return
                        if cursor < len(state.events):
                            raw_event = state.events[cursor]
                            cursor += 1
                        else:
                            return

                if raw_event is None:
                    try:
                        raw_event = await overflow_q.get()
                    except asyncio.CancelledError:
                        raise
                    if raw_event is None:
                        return

                # H2: Authoritative validation immediately before EVERY delivery (replay AND live).
                filtered, revoked, is_official_chunk = await filter_sse_event_for_replay(raw_event)
                if is_official_chunk:
                    had_non_community_chunk = True
                if revoked:
                    any_revoked = True
                if filtered is None:
                    continue

                _, parsed = _split_sse_event(filtered)
                if parsed and parsed.get("type") == "chunk":
                    had_any_surviving_chunk = True

                if any_revoked and parsed and parsed.get("type") == "done":
                    if had_non_community_chunk or had_any_surviving_chunk:
                        yield filtered
                        return
                    yield community_revoked_replay_error()
                    return

                yield filtered
                if parsed and parsed.get("type") in {"done", "error"}:
                    return
        finally:
            async with self._guard:
                state.active_subscribers = max(0, state.active_subscribers - 1)
                state.overflow_queues.discard(overflow_q)

    async def _pump(
        self,
        key: str,
        state: _RequestState,
        factory: Callable[[], AsyncIterator[str]],
    ) -> None:
        try:
            async for event in factory():
                encoded_size = len(event.encode("utf-8"))
                if event.startswith("data:"):
                    try:
                        parsed = json.loads(event[5:].strip())
                        if isinstance(parsed, dict):
                            _, refs, _ = extract_community_refs_from_payload(parsed)
                            for ref in refs:
                                state.community_ids.add(ref["community_id"])
                    except ValueError:
                        pass
                async with state.cond:
                    if state.replayable and state.replay_bytes + encoded_size <= self._max_replay_bytes:
                        state.events.append(event)
                        state.replay_bytes += encoded_size
                    else:
                        if state.replayable:
                            state.replayable = False
                            state.events.clear()
                            state.replay_bytes = 0
                        for q in tuple(state.overflow_queues):
                            try:
                                q.put_nowait(event)
                            except asyncio.QueueFull:
                                pass
                    state.cond.notify_all()
        finally:
            async with state.cond:
                state.completed_at = time.monotonic()
                for q in tuple(state.overflow_queues):
                    try:
                        q.put_nowait(None)
                    except asyncio.QueueFull:
                        pass
                state.cond.notify_all()
            async with self._guard:
                if self._states.get(key) is state:
                    self._states.move_to_end(key)
                    self._prune(state.completed_at)

    def invalidate_community_replay(self, community_id: str) -> int:
        """Mark local replay states referencing a retracted Community record."""
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
            and state.active_subscribers == 0
            and now - state.completed_at >= self._replay_ttl_seconds
        ]
        for key in expired:
            self._states.pop(key, None)
        while len(self._states) > self._max_entries:
            key, state = next(iter(self._states.items()))
            if state.completed_at is None or state.active_subscribers > 0:
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
