"""Redis-backed request ownership, fenced SSE replay, and distributed limits."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
import logging

from redis.exceptions import RedisError

from .community_replay_guard import (
    _split_sse_event,
    community_revoked_replay_error,
    extract_community_refs_from_payload,
    filter_sse_event_for_replay,
)

from ..logger import generation_fence_var

logger = logging.getLogger(__name__)


ACQUIRE_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local status = redis.call('HGET', KEYS[1], 'status')
local fingerprint = redis.call('HGET', KEYS[1], 'fingerprint')
if fingerprint and ARGV[6] ~= '' and fingerprint ~= ARGV[6] then return {4, 0} end
if not status and ARGV[5] == '1' then return {3, 0} end
if status == 'completed' then return {2, tonumber(redis.call('HGET', KEYS[1], 'fence') or '0')} end
local lease = tonumber(redis.call('HGET', KEYS[1], 'lease_until') or '0')
if status == 'running' and lease > now then
  return {0, tonumber(redis.call('HGET', KEYS[1], 'fence') or '0')}
end
local fence = redis.call('INCR', KEYS[2])
redis.call('HSET', KEYS[1], 'status', 'running', 'owner', ARGV[1], 'fence', fence,
  'lease_until', now + tonumber(ARGV[3]))
if ARGV[6] ~= '' then redis.call('HSET', KEYS[1], 'fingerprint', ARGV[6]) end
redis.call('PEXPIRE', KEYS[1], ARGV[4])
redis.call('PEXPIRE', KEYS[2], ARGV[4])
return {1, fence}
"""

RENEW_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if redis.call('HGET', KEYS[1], 'owner') ~= ARGV[1] or
   redis.call('HGET', KEYS[1], 'fence') ~= ARGV[2] or
   tonumber(redis.call('HGET', KEYS[1], 'lease_until') or '0') <= now then return 0 end
redis.call('HSET', KEYS[1], 'lease_until', now + tonumber(ARGV[4]))
redis.call('PEXPIRE', KEYS[1], ARGV[5])
redis.call('PEXPIRE', KEYS[2], ARGV[5])
return 1
"""

PUBLISH_LUA = """
if redis.call('HGET', KEYS[1], 'status') ~= 'running' or
   redis.call('HGET', KEYS[1], 'owner') ~= ARGV[1] or
   redis.call('HGET', KEYS[1], 'fence') ~= ARGV[2] then return '' end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if tonumber(redis.call('HGET', KEYS[1], 'lease_until') or '0') <= now then return '' end
local id = redis.call('XADD', KEYS[2], 'MAXLEN', '=', ARGV[4], '*',
  'event', ARGV[3], 'fence', ARGV[2])
redis.call('HSETNX', KEYS[1], 'first_id', id)
redis.call('PEXPIRE', KEYS[2], ARGV[5])
if ARGV[6] == '1' then
  redis.call('HSET', KEYS[1], 'status', 'completed', 'terminal_id', id)
  redis.call('PEXPIRE', KEYS[1], ARGV[5])
  redis.call('PEXPIRE', KEYS[3], ARGV[5])
end
return id
"""

RATE_LIMIT_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local window = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')[2]
  return math.max(1, tonumber(oldest) + window - now)
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], window)
return 0
"""

EPOCH_LUA = """
local function epoch_valid(key, expected)
  if expected == '' then return true end
  local marker = redis.call('GET', key)
  if not marker then return false end
  local run_id = string.match(redis.call('INFO', 'server'), 'run_id:([a-f0-9]+)')
  return run_id and marker == expected .. ':' .. run_id
end
"""

CHAT_ADMISSION_LUA = EPOCH_LUA + """
if not epoch_valid(KEYS[3], ARGV[5]) then return {-1, 0} end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local window = tonumber(ARGV[1])
for i = 1, 2 do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now - window)
  if redis.call('ZCARD', KEYS[i]) >= tonumber(ARGV[i + 1]) then
    local oldest = redis.call('ZRANGE', KEYS[i], 0, 0, 'WITHSCORES')[2]
    return {i, math.max(1, tonumber(oldest) + window - now)}
  end
end
for i = 1, 2 do
  redis.call('ZADD', KEYS[i], now, ARGV[4])
  redis.call('PEXPIRE', KEYS[i], window)
end
return {0, 0}
"""

ROLLING_BUDGET_LUA = EPOCH_LUA + """
if not epoch_valid(KEYS[3], ARGV[6]) then return {-1, 0} end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local window = tonumber(ARGV[4])
local tally = redis.call('GET', KEYS[2])
local count = redis.call('ZCARD', KEYS[1])
if count > 0 and not tally then return {-1, 0} end
local tokens = count == 0 and 0 or tonumber(tally)
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now - window)
for _, item in ipairs(expired) do
  tokens = tokens - tonumber(string.match(item, '^%d+'))
end
if #expired > 0 then
  redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
  redis.call('SET', KEYS[2], tokens, 'PX', window * 2)
end
local charge = tonumber(ARGV[3])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) or tokens + charge > tonumber(ARGV[2]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')[2]
  return {0, oldest and math.max(1, tonumber(oldest) + window - now) or window}
end
redis.call('ZADD', KEYS[1], now, ARGV[3] .. ':' .. ARGV[5])
redis.call('SET', KEYS[2], tokens + charge, 'PX', window * 2)
redis.call('PEXPIRE', KEYS[1], window * 2)
return {1, 0}
"""

SUBSCRIBERS_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
for i = 1, 2 do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now)
  if redis.call('ZCARD', KEYS[i]) >= tonumber(ARGV[i]) then return 0 end
end
for i = 1, 2 do
  redis.call('ZADD', KEYS[i], now + tonumber(ARGV[3]), ARGV[4])
  redis.call('PEXPIRE', KEYS[i], ARGV[3])
end
return 1
"""

SEMAPHORE_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], tonumber(ARGV[1]) + tonumber(ARGV[2]), ARGV[4])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""

CAPACITY_ACQUIRE_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
for i = 1, 4 do redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now) end
if redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[7]) then return -1 end
local queued = redis.call('ZSCORE', KEYS[2], ARGV[4])
local head = redis.call('ZRANGE', KEYS[2], 0, 0)[1]
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) and
   (not head or head == ARGV[4]) then
  redis.call('ZREM', KEYS[2], ARGV[4])
  redis.call('ZREM', KEYS[4], ARGV[4])
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[4])
  redis.call('ZADD', KEYS[3], now + tonumber(ARGV[3]), ARGV[4])
  redis.call('PEXPIRE', KEYS[1], ARGV[3])
  redis.call('PEXPIRE', KEYS[3], ARGV[3])
  return 1
end
if not queued then
  if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[5]) or
     redis.call('ZCARD', KEYS[4]) >= tonumber(ARGV[8]) then return -1 end
  local expires = now + tonumber(ARGV[6]) / 2
  for _, key in ipairs({KEYS[2], KEYS[4]}) do
    redis.call('ZADD', key, expires, ARGV[4])
    redis.call('PEXPIRE', key, ARGV[6])
  end
end
return 0
"""

CAPACITY_RENEW_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local current = tonumber(redis.call('ZSCORE', KEYS[1], ARGV[1]) or '0')
if current <= now then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[3]), ARGV[1])
redis.call('PEXPIRE', KEYS[2], ARGV[3])
return 1
"""


@dataclass(frozen=True)
class Ownership:
    leader: bool
    fence: int
    completed: bool = False
    expired: bool = False
    conflict: bool = False


class RedisCoordinator:
    """At-most-one fenced publisher; external AI billing is not exactly-once."""

    def __init__(self, redis, *, reader=None, prefix="kuniv", lease_ms=30_000, replay_ttl_ms=300_000, max_events=2048):
        self.redis = redis
        self.reader = reader if reader is not None else redis
        self.prefix = prefix
        self.lease_ms = lease_ms
        self.replay_ttl_ms = replay_ttl_ms
        self.max_events = max_events
        self.owner = str(uuid.uuid4())
        self._pumps: set[asyncio.Task] = set()

    def _keys(self, request_key: str) -> tuple[str, str, str]:
        digest = hashlib.sha256(request_key.encode()).hexdigest()
        base = f"{self.prefix}:request:{digest}"
        return f"{base}:meta", f"{base}:fence", f"{base}:events"

    async def acquire(self, request_key: str, *, resume: bool = False, fingerprint: str = "") -> Ownership:
        meta, fence_key, _ = self._keys(request_key)
        now = int(time.time() * 1000)
        result = await self.redis.eval(
            ACQUIRE_LUA, 2, meta, fence_key, self.owner, now,
            self.lease_ms, self.replay_ttl_ms, int(resume), fingerprint,
        )
        code, fence = int(result[0]), int(result[1])
        return Ownership(code == 1, fence, code == 2, code == 3, code == 4)

    @staticmethod
    def _error(code: str, message: str, *, retryable: bool = True) -> str:
        return "data: " + json.dumps({
            "type": "error", "code": code, "retryable": retryable, "message": message,
        }) + "\n\n"

    @staticmethod
    def _terminal(event: str) -> bool:
        for line in event.splitlines():
            if line.startswith("data:"):
                try:
                    payload = json.loads(line[5:])
                except ValueError:
                    return False
                return isinstance(payload, dict) and payload.get("type") in {"done", "error"}
        return False

    async def stream(self, request_key, factory, *, last_event_id="0-0", fingerprint=""):
        try:
            async with aclosing(self._stream(
                request_key, factory, last_event_id=last_event_id, fingerprint=fingerprint,
            )) as stream:
                async for event in stream:
                    yield event
        except RedisError as exc:
            logger.warning("SSE Redis operation failed (%s)", type(exc).__name__)
            yield self._error("AI_COORDINATION_UNAVAILABLE", "انقطع الاتصال بخدمة المحادثة. أعد المحاولة.")

    async def _stream(
        self,
        request_key: str,
        factory: Callable[[], AsyncIterator[str]],
        *,
        last_event_id: str = "0-0",
        fingerprint: str = "",
    ) -> AsyncIterator[str]:
        meta, _, events = self._keys(request_key)
        cursor = last_event_id or "0-0"
        if cursor != "0-0" and not await self.redis.xrange(events, min=cursor, max=cursor, count=1):
            yield self._error("SSE_REPLAY_UNAVAILABLE", "انتهت صلاحية سجل البث.", retryable=False)
            return

        ownership = await self.acquire(request_key, resume=cursor != "0-0", fingerprint=fingerprint)
        if ownership.conflict:
            yield self._error("MESSAGE_ID_CONFLICT", "معرّف الرسالة مستخدم لمحتوى مختلف.", retryable=False)
            return
        if ownership.expired:
            yield self._error("SSE_REPLAY_UNAVAILABLE", "انتهت صلاحية سجل البث.", retryable=False)
            return
        if ownership.leader:
            await self._start_pump(request_key, ownership.fence, factory)

        heartbeat_at = time.monotonic() + 10
        had_non_community_chunk = False
        any_community_revoked = False
        while True:
            state = await self.redis.hgetall(meta)
            status = state.get(b"status", state.get("status"))
            first_id = state.get(b"first_id", state.get("first_id"))
            # H3: Read persisted section state so mid-stream resume after official chunk preserves mixed terminal.
            persisted_official = state.get(b"has_official_chunk", state.get("has_official_chunk"))
            if persisted_official in (b"1", "1"):
                had_non_community_chunk = True
            checkpoint = (first_id.decode() if isinstance(first_id, bytes) else first_id) if cursor == "0-0" else cursor
            if checkpoint and not await self.redis.xrange(events, min=checkpoint, max=checkpoint, count=1):
                yield self._error("SSE_REPLAY_UNAVAILABLE", "لم يعد سجل البث كاملاً.", retryable=False)
                return
            if status in ("completed", b"completed"):
                rows = await self.redis.xread({events: cursor}, count=100)
                if not rows:
                    if not state.get(b"terminal_id", state.get("terminal_id")):
                        yield self._error("SSE_REPLAY_UNAVAILABLE", "سجل البث غير مكتمل.", retryable=False)
                    return
            elif not status:
                yield self._error("SSE_REPLAY_UNAVAILABLE", "انتهت صلاحية سجل البث.", retryable=False)
                return
            else:
                rows = await self.reader.xread({events: cursor}, count=100, block=1000)
            for _, entries in rows:
                for event_id, fields in entries:
                    cursor = event_id.decode() if isinstance(event_id, bytes) else event_id
                    raw = fields.get(b"event", fields.get("event"))
                    event = raw.decode() if isinstance(raw, bytes) else raw
                    try:
                        filtered_event, revoked, is_official_chunk = await filter_sse_event_for_replay(event)
                    except Exception:
                        yield f"id: {cursor}\n{community_revoked_replay_error()}"
                        return
                    if is_official_chunk:
                        had_non_community_chunk = True
                    if revoked:
                        any_community_revoked = True
                    if filtered_event is None:
                        continue
                    # H3: Emit ONE atomic redacted terminal event using the unique stream cursor ID.
                    if any_community_revoked and self._terminal(filtered_event):
                        if had_non_community_chunk:
                            yield f"id: {cursor}\n{filtered_event}"
                            return
                        yield f"id: {cursor}\n{community_revoked_replay_error()}"
                        return
                    yield f"id: {cursor}\n{filtered_event}"
                    if self._terminal(filtered_event):
                        return
                    heartbeat_at = time.monotonic() + 10
            if time.monotonic() >= heartbeat_at:
                yield ": heartbeat\n\n"
                heartbeat_at = time.monotonic() + 10
            if status in ("running", b"running") and not rows:
                ownership = await self.acquire(request_key, resume=True, fingerprint=fingerprint)
                if ownership.leader:
                    await self._start_pump(request_key, ownership.fence, factory)

    async def _start_pump(self, request_key, fence, factory):
        if fence > 1:
            reset = "data: " + json.dumps({"type": "reset", "fence": fence}) + "\n\n"
            if not await self._publish(request_key, fence, reset):
                return
        task = asyncio.create_task(self._pump(request_key, fence, factory))
        self._pumps.add(task)
        task.add_done_callback(self._pumps.discard)

    async def _pump(self, request_key: str, fence: int, factory) -> None:
        token = generation_fence_var.set(fence)
        renewer = asyncio.create_task(self._renew_loop(request_key, fence, asyncio.current_task()))
        try:
            meta, _, _ = self._keys(request_key)
            async with aclosing(factory()) as stream:
                async for event in stream:
                    _, parsed = _split_sse_event(event)
                    if parsed and parsed.get("type") == "chunk":
                        is_comm, _, _ = extract_community_refs_from_payload(parsed)
                        if not is_comm:
                            await self.redis.hset(meta, "has_official_chunk", "1")
                    if not await self._publish(request_key, fence, event) or self._terminal(event):
                        return
            await self._publish(request_key, fence, self._error(
                "GENERATION_STREAM_INCOMPLETE", "انتهت معالجة الإجابة قبل اكتمالها.",
                retryable=False,
            ))
        except RedisError as exc:
            logger.warning("SSE Redis publication failed (%s)", type(exc).__name__)
        except Exception as exc:
            logger.warning("SSE generation failed (%s)", type(exc).__name__)
            try:
                await self._publish(request_key, fence, self._error(
                    "GENERATION_WORKER_FAILED", "تعطلت معالجة الإجابة.", retryable=False,
                ))
            except RedisError as exc:
                logger.warning("SSE Redis failure publication failed (%s)", type(exc).__name__)
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
            generation_fence_var.reset(token)

    async def _renew_loop(self, request_key: str, fence: int, pump: asyncio.Task) -> None:
        meta, fence_key, _ = self._keys(request_key)
        while True:
            await asyncio.sleep(self.lease_ms / 3000)
            now = int(time.time() * 1000)
            try:
                renewed = await self.redis.eval(
                    RENEW_LUA, 2, meta, fence_key, self.owner, fence, now,
                    self.lease_ms, self.replay_ttl_ms,
                )
            except RedisError:
                renewed = False
            if not renewed:
                pump.cancel()
                return

    async def _publish(self, request_key: str, fence: int, event: str) -> bool:
        meta, fence_key, events = self._keys(request_key)
        result = await self.redis.eval(
            PUBLISH_LUA, 3, meta, events, fence_key, self.owner, fence, event,
            self.max_events, self.replay_ttl_ms, int(self._terminal(event)),
        )
        return bool(result)

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def shutdown(self, timeout_seconds: float = 20.0) -> None:
        """Drain active pumps; cancelled work is recoverable after lease expiry."""
        if not self._pumps:
            return
        _, pending = await asyncio.wait(self._pumps, timeout=timeout_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class RedisUserRateLimiter:
    def __init__(self, redis, *, prefix: str, requests: int, window_seconds: int):
        self.redis, self.prefix = redis, prefix
        self.requests, self.window_ms = requests, window_seconds * 1000

    async def check(self, uid: str) -> int | None:
        key = f"{self.prefix}:rate:user:{hashlib.sha256(uid.encode()).hexdigest()}"
        delay = int(await self.redis.eval(RATE_LIMIT_LUA, 1, key, self.window_ms, self.requests, uuid.uuid4().hex))
        return (delay + 999) // 1000 if delay else None


class RedisChatAdmission:
    def __init__(self, redis, *, prefix: str, user_rpm: int, global_rpm: int, epoch_key: str, epoch: str):
        if min(user_rpm, global_rpm) < 1:
            raise ValueError("Chat request budgets must be positive")
        self.redis = redis
        self.prefix = f"{prefix}:chat-admission"
        self.user_rpm, self.global_rpm = user_rpm, global_rpm
        self.epoch_key, self.epoch = epoch_key, epoch

    async def reserve(self, uid: str) -> tuple[str, int] | None:
        digest = hashlib.sha256(uid.encode()).hexdigest()
        code, delay = await self.redis.eval(
            CHAT_ADMISSION_LUA, 3, f"{self.prefix}:{digest}", f"{self.prefix}:global", self.epoch_key,
            60_000, self.user_rpm, self.global_rpm, uuid.uuid4().hex, self.epoch,
        )
        if code == -1:
            raise RedisError("Coordination epoch missing or changed")
        if code:
            return ("AI_USER_RATE_LIMITED" if code == 1 else "AI_GLOBAL_RATE_LIMITED", (delay + 999) // 1000)
        return None


class RedisOpenAIBudget:
    """Atomic rolling reservation. Attempt charges remain until the window expires."""

    def __init__(self, redis, *, prefix: str, rpm: int, tpm: int, epoch: str = "", epoch_key: str | None = None):
        if rpm < 1 or tpm < 1:
            raise ValueError("RPM and TPM budgets must be positive")
        self.redis, self.prefix, self.rpm, self.tpm = redis, prefix, rpm, tpm
        self.epoch = epoch
        self.epoch_key = epoch_key or f"{prefix}:epoch"

    async def reserve(self, estimated_tokens: int) -> int | None:
        key = f"{self.prefix}:rolling"
        accepted, remaining_ms = await self.redis.eval(
            ROLLING_BUDGET_LUA, 3, key, f"{key}:tokens", self.epoch_key,
            self.rpm, self.tpm, max(0, estimated_tokens), 60_000,
            uuid.uuid4().hex, self.epoch,
        )
        if int(accepted) == -1:
            raise RedisError("Coordination epoch missing or changed")
        return None if int(accepted) == 1 else max(1, (int(remaining_ms) + 999) // 1000)


class RedisSubscriberGate:
    def __init__(self, redis, *, prefix: str, limit: int, per_user: int, local_limit: int, seconds: int):
        self.redis, self.prefix, self.limit, self.per_user = redis, prefix, limit, per_user
        self.local = asyncio.BoundedSemaphore(local_limit)
        self.seconds = seconds

    async def stream(self, uid: str, factory) -> AsyncIterator[str]:
        if self.local.locked():
            yield RedisCoordinator._error("SSE_CAPACITY_EXCEEDED", "الاتصالات مشغولة. حاول بعد قليل.")
            return
        async with self.local:
            token = uuid.uuid4().hex
            user = hashlib.sha256(uid.encode()).hexdigest()
            keys = (f"{self.prefix}:sse", f"{self.prefix}:sse:{user}")
            acquired = False
            try:
                acquired = bool(await self.redis.eval(
                    SUBSCRIBERS_LUA, 2, *keys, self.limit, self.per_user,
                    (self.seconds + 10) * 1000, token,
                ))
                if not acquired:
                    yield RedisCoordinator._error("SSE_CAPACITY_EXCEEDED", "الاتصالات مشغولة. حاول بعد قليل.")
                    return
                async with asyncio.timeout(self.seconds):
                    async with aclosing(factory()) as stream:
                        async for event in stream:
                            yield event
            except TimeoutError:
                yield RedisCoordinator._error("SSE_SUBSCRIPTION_EXPIRED", "أعد الاتصال لاستكمال المحادثة.")
            except RedisError:
                yield RedisCoordinator._error("AI_COORDINATION_UNAVAILABLE", "خدمة المحادثة غير متاحة مؤقتاً.")
            finally:
                if acquired:
                    try:
                        async with self.redis.pipeline(transaction=True) as pipe:
                            for key in keys:
                                pipe.zrem(key, token)
                            await pipe.execute()
                    except RedisError:
                        logger.warning("SSE subscriber cleanup deferred until lease expiry")


class RedisSemaphore:
    def __init__(self, redis, *, key: str, limit: int, lease_ms: int):
        self.redis, self.key, self.limit, self.lease_ms = redis, key, limit, lease_ms

    async def acquire(self, token: str) -> bool:
        return bool(await self.redis.eval(
            SEMAPHORE_ACQUIRE_LUA, 1, self.key, int(time.time() * 1000),
            self.lease_ms, self.limit, token,
        ))

    async def release(self, token: str) -> None:
        await self.redis.zrem(self.key, token)


class RedisCapacityGate:
    """Fair, lease-backed global concurrency and bounded distributed queue."""

    def __init__(
        self, redis, *, prefix: str, limit: int, max_queued: int,
        wait_seconds: float, lease_ms: int, per_user: int | None = None, queued_per_user: int = 2,
    ):
        self.redis, self.limit, self.max_queued = redis, limit, max_queued
        self.wait_seconds, self.lease_ms = wait_seconds, lease_ms
        self.active_key = f"{prefix}:capacity:active"
        self.queue_key = f"{prefix}:capacity:queue"
        self.per_user = per_user if per_user is not None else limit
        self.queued_per_user = queued_per_user

    async def stream(self, factory: Callable[[], AsyncIterator[str]], *, uid: str = "") -> AsyncIterator[str]:
        token = str(uuid.uuid4())
        deadline = time.monotonic() + self.wait_seconds
        acquired = False
        user_key = f"{self.active_key}:{hashlib.sha256(uid.encode()).hexdigest()}"
        user_queue = f"{user_key}:queued"
        try:
            while time.monotonic() < deadline:
                now = int(time.time() * 1000)
                result = int(await self.redis.eval(
                    CAPACITY_ACQUIRE_LUA, 4, self.active_key, self.queue_key, user_key, user_queue,
                    now, self.limit, self.lease_ms, token, self.max_queued,
                    int(self.wait_seconds * 2000), self.per_user, self.queued_per_user,
                ))
                if result == 1:
                    acquired = True
                    break
                if result == -1:
                    break
                await asyncio.sleep(.05)
        finally:
            if not acquired:
                await self._remove(token, self.queue_key, user_queue)
        if not acquired:
            yield "data: " + json.dumps({
                "type": "error", "code": "AI_CAPACITY_EXCEEDED",
                "retryable": True, "retry_after_seconds": max(1, int(self.wait_seconds)),
                "message": "الخدمة مشغولة حالياً. حاول مرة أخرى بعد قليل.",
            }) + "\n\n"
            return
        renewer = asyncio.create_task(self._renew(token, user_key, asyncio.current_task()))
        try:
            async with aclosing(factory()) as stream:
                async for event in stream:
                    yield event
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
            await self._remove(token, self.active_key, user_key)

    async def _remove(self, token: str, *keys: str) -> None:
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                for key in keys:
                    pipe.zrem(key, token)
                await pipe.execute()
        except RedisError:
            logger.warning("Capacity cleanup deferred until lease expiry")

    async def _renew(self, token: str, user_key: str, pump: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(self.lease_ms / 3000)
            now = int(time.time() * 1000)
            try:
                renewed = await self.redis.eval(
                    CAPACITY_RENEW_LUA, 2, self.active_key, user_key, token, now, self.lease_ms,
                )
            except RedisError:
                renewed = False
            if not renewed:
                pump.cancel()
                return
