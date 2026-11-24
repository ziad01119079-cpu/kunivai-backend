"""Bounded single-process admission control for AI chat generation."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable


class CapacityExceeded(Exception):
    pass


class AICapacityGate:
    def __init__(self, *, max_active: int, max_queued: int, queue_timeout: float):
        if max_active < 1 or max_queued < 0 or queue_timeout <= 0:
            raise ValueError("invalid AI capacity configuration")
        self.max_active = max_active
        self.max_queued = max_queued
        self.queue_timeout = queue_timeout
        self._active = 0
        self._queued = 0
        self._condition = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._condition:
            if self._active < self.max_active:
                self._active += 1
                return
            if self._queued >= self.max_queued:
                raise CapacityExceeded
            self._queued += 1
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._active < self.max_active),
                    timeout=self.queue_timeout,
                )
                self._active += 1
            except asyncio.TimeoutError as exc:
                raise CapacityExceeded from exc
            finally:
                self._queued -= 1

    async def release(self) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify(1)

    async def stream(self, factory: Callable[[], AsyncIterator[str]]) -> AsyncIterator[str]:
        try:
            await self.acquire()
        except CapacityExceeded:
            yield "data: " + json.dumps({
                "type": "error",
                "code": "AI_CAPACITY_EXCEEDED",
                "retryable": True,
                "retry_after_seconds": max(1, int(self.queue_timeout)),
                "message": "الخدمة مشغولة حالياً. حاول مرة أخرى بعد قليل.",
            }) + "\n\n"
            return
        try:
            async for event in factory():
                yield event
        finally:
            await self.release()


ai_capacity_gate = AICapacityGate(
    max_active=int(os.getenv("AI_MAX_CONCURRENT_GENERATIONS", "20")),
    max_queued=int(os.getenv("AI_MAX_QUEUED_GENERATIONS", "40")),
    queue_timeout=float(os.getenv("AI_QUEUE_TIMEOUT_SECONDS", "5")),
)
