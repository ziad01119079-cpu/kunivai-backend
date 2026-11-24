"""Select local or Redis coordination entirely through configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from .distributed_coordination import (
    RedisCapacityGate,
    RedisCoordinator,
    RedisOpenAIBudget,
    RedisSubscriberGate,
    RedisUserRateLimiter,
    EPOCH_LUA,
)


@dataclass(frozen=True)
class RedisRuntime:
    client: object
    reader: object
    requests: RedisCoordinator
    capacity: RedisCapacityGate
    users: RedisUserRateLimiter
    openai_budget: RedisOpenAIBudget
    starts: RedisOpenAIBudget
    subscribers: RedisSubscriberGate
    epoch_key: str
    epoch: str

    async def epoch_ready(self) -> bool:
        if not self.epoch:
            return bool(await self.client.ping())
        return bool(await self.client.eval(
            EPOCH_LUA + "return epoch_valid(KEYS[1], ARGV[1]) and 1 or 0",
            1, self.epoch_key, self.epoch,
        ))


def distributed_enabled() -> bool:
    return os.getenv("COORDINATION_BACKEND", "memory").casefold() == "redis"


@lru_cache(maxsize=1)
def get_redis_runtime() -> RedisRuntime:
    if not distributed_enabled():
        raise RuntimeError("Redis coordination is not enabled")
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        raise RuntimeError("REDIS_URL is required for distributed coordination")
    import redis.asyncio as redis

    options = dict(
        decode_responses=False,
        socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT_SECONDS", "2")),
        socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "3")),
        health_check_interval=30,
    )
    prefix = os.getenv("REDIS_KEY_PREFIX", "kuniv")
    epoch = os.getenv("AI_REDIS_EPOCH", "")
    epoch_key = f"{prefix}:epoch"
    local_sse = int(os.getenv("AI_SSE_PER_WORKER", "128"))
    max_active = int(os.getenv("AI_MAX_CONCURRENT_GENERATIONS", "20"))
    lease_ms = int(os.getenv("AI_REQUEST_LEASE_MS", "30000"))
    replay_ms = int(os.getenv("AI_REPLAY_TTL_SECONDS", "300")) * 1000
    if min(local_sse, max_active, lease_ms) < 1 or replay_ms <= lease_ms:
        raise ValueError("Redis leases, replay retention and subscriber capacity are invalid")
    client = redis.from_url(url, max_connections=int(os.getenv("AI_REDIS_CONTROL_CONNECTIONS", "64")), **options)
    reader = redis.from_url(url, max_connections=local_sse, **options)
    return RedisRuntime(
        client=client,
        reader=reader,
        requests=RedisCoordinator(
            client, prefix=prefix, reader=reader,
            lease_ms=lease_ms,
            replay_ttl_ms=replay_ms,
            max_events=int(os.getenv("AI_REPLAY_MAX_EVENTS", "2048")),
        ),
        capacity=RedisCapacityGate(
            client, prefix=prefix,
            limit=max_active,
            max_queued=int(os.getenv("AI_MAX_QUEUED_GENERATIONS", "40")),
            wait_seconds=float(os.getenv("AI_QUEUE_TIMEOUT_SECONDS", "5")),
            lease_ms=int(os.getenv("AI_CAPACITY_LEASE_MS", "120000")),
            per_user=int(os.getenv("AI_MAX_GENERATIONS_PER_USER", "2")),
            queued_per_user=int(os.getenv("AI_MAX_QUEUED_PER_USER", "2")),
        ),
        users=RedisUserRateLimiter(
            client, prefix=prefix,
            requests=int(os.getenv("AI_USER_RATE_LIMIT_REQUESTS", "20")),
            window_seconds=int(os.getenv("AI_USER_RATE_LIMIT_WINDOW_SECONDS", "60")),
        ),
        openai_budget=RedisOpenAIBudget(
            client, prefix=f"{prefix}:provider", epoch=epoch, epoch_key=epoch_key,
            rpm=min(5000, int(os.getenv("OPENAI_RPM_BUDGET", "5000"))),
            tpm=min(450000, int(os.getenv("OPENAI_TPM_BUDGET", "450000"))),
        ),
        starts=RedisOpenAIBudget(
            client, prefix=f"{prefix}:generation-start", epoch=epoch, epoch_key=epoch_key,
            rpm=int(os.getenv("AI_GLOBAL_GENERATION_RPM", "300")), tpm=1,
        ),
        subscribers=RedisSubscriberGate(
            client, prefix=prefix, local_limit=local_sse,
            limit=int(os.getenv("AI_SSE_GLOBAL", "512")),
            per_user=int(os.getenv("AI_SSE_PER_USER", "4")),
            seconds=int(os.getenv("AI_SSE_MAX_SECONDS", "180")),
        ),
        epoch=epoch, epoch_key=epoch_key,
    )


async def redis_ready() -> bool:
    if not distributed_enabled():
        return True
    try:
        return await get_redis_runtime().epoch_ready()
    except Exception:
        return False


async def enforce_route_rate(uid: str, route: str, requests: int) -> None:
    """Shared UID guard for secondary authenticated routes in Redis mode."""
    if not distributed_enabled():
        return
    from fastapi import HTTPException
    from redis.exceptions import RedisError
    runtime = get_redis_runtime()
    try:
        if runtime.epoch and not await runtime.epoch_ready():
            raise RedisError("Coordination epoch missing or changed")
        limiter = RedisUserRateLimiter(
            runtime.client, prefix=f"{runtime.requests.prefix}:{route}", requests=requests, window_seconds=60,
        )
        delay = await limiter.check(uid)
    except RedisError:
        raise HTTPException(503, "Admission unavailable", headers={"Retry-After": "2"}) from None
    if delay:
        raise HTTPException(429, "User request limit exceeded", headers={"Retry-After": str(delay)})


async def close_redis() -> None:
    if distributed_enabled():
        runtime = get_redis_runtime()
        await runtime.requests.shutdown()
        await runtime.reader.aclose()
        await runtime.client.aclose()
        get_redis_runtime.cache_clear()
