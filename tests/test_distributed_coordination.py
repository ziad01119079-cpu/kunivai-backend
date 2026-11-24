import asyncio

import fakeredis.aioredis

from app.services.distributed_coordination import (
    RedisCapacityGate,
    RedisCoordinator,
    RedisOpenAIBudget,
    RedisUserRateLimiter,
)


async def _collect(stream):
    return [event async for event in stream]


def async_test(function):
    def wrapper():
        asyncio.run(function())

    return wrapper


@async_test
async def test_same_request_is_generated_once_across_coordinators():
    redis = fakeredis.aioredis.FakeRedis()
    first = RedisCoordinator(redis, prefix="test")
    second = RedisCoordinator(redis, prefix="test")
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        yield "event: token\ndata: one\n\n"
        yield 'data: {"type":"done"}\n\n'

    left, right = await asyncio.gather(
        _collect(first.stream("u:s:m", factory)),
        _collect(second.stream("u:s:m", factory)),
    )

    assert calls == 1
    assert left == right
    assert any('"type":"done"' in event for event in left)


@async_test
async def test_completed_response_replays_without_new_generation():
    redis = fakeredis.aioredis.FakeRedis()
    coordinator = RedisCoordinator(redis, prefix="test")
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        yield "data: answer\n\n"
        yield 'data: {"type":"done"}\n\n'

    original = await _collect(coordinator.stream("u:s:m", factory))
    replay = await _collect(coordinator.stream("u:s:m", factory))

    assert calls == 1
    assert replay == original


@async_test
async def test_reconnect_uses_last_event_id():
    redis = fakeredis.aioredis.FakeRedis()
    coordinator = RedisCoordinator(redis, prefix="test")

    async def factory():
        yield "data: first\n\n"
        yield 'data: {"type":"done","content":"second"}\n\n'

    events = await _collect(coordinator.stream("u:s:m", factory))
    first_id = events[0].splitlines()[0].removeprefix("id: ")
    resumed = await _collect(
        coordinator.stream("u:s:m", factory, last_event_id=first_id)
    )

    assert len(resumed) == 1
    assert "second" in resumed[0]


@async_test
async def test_expired_owner_gets_new_fence_and_stale_publish_is_rejected():
    redis = fakeredis.aioredis.FakeRedis()
    first = RedisCoordinator(redis, prefix="test", lease_ms=20)
    second = RedisCoordinator(redis, prefix="test", lease_ms=20)
    ownership = await first.acquire("u:s:m")
    await asyncio.sleep(0.03)
    replacement = await second.acquire("u:s:m")

    assert ownership.leader
    assert replacement.leader
    assert replacement.fence > ownership.fence
    assert not await first._publish("u:s:m", ownership.fence, "data: stale\n\n")
    assert await second._publish("u:s:m", replacement.fence, "data: current\n\n")
    assert not await first._publish("u:s:m", ownership.fence, 'data: {"type":"done"}\n\n')


@async_test
async def test_terminal_publish_atomically_prevents_takeover():
    redis = fakeredis.aioredis.FakeRedis()
    first = RedisCoordinator(redis, prefix="test", lease_ms=500)
    second = RedisCoordinator(redis, prefix="test", lease_ms=500)
    owner = await first.acquire("u:s:m")
    assert await first._publish("u:s:m", owner.fence, 'data: {"type":"done"}\n\n')
    meta, _, _ = first._keys("u:s:m")
    await redis.hset(meta, "lease_until", 0)
    assert (await second.acquire("u:s:m")).completed
    assert not await first._publish("u:s:m", owner.fence, 'data: {"type":"done"}\n\n')


@async_test
async def test_expired_cursor_does_not_start_generation():
    redis = fakeredis.aioredis.FakeRedis()
    coordinator = RedisCoordinator(redis, prefix="test")

    async def factory():
        raise AssertionError("expired resume must not start a new generation")
        yield

    result = await _collect(coordinator.stream("u:s:m", factory, last_event_id="123-0"))
    assert len(result) == 1
    assert "SSE_REPLAY_UNAVAILABLE" in result[0]


@async_test
async def test_unexpected_provider_eof_replays_explicit_failure():
    redis = fakeredis.aioredis.FakeRedis()
    coordinator = RedisCoordinator(redis, prefix="test")

    async def incomplete():
        yield 'data: {"type":"chunk","content":"partial"}\n\n'

    result = await _collect(coordinator.stream("u:s:m", incomplete))
    assert len(result) == 2
    assert "GENERATION_STREAM_INCOMPLETE" in result[-1]
    assert await _collect(coordinator.stream("u:s:m", incomplete)) == result


@async_test
async def test_user_rate_limit_is_shared():
    redis = fakeredis.aioredis.FakeRedis()
    one = RedisUserRateLimiter(redis, prefix="test", requests=2, window_seconds=60)
    two = RedisUserRateLimiter(redis, prefix="test", requests=2, window_seconds=60)

    assert await one.check("user") is None
    assert await two.check("user") is None
    assert await one.check("user") is not None


@async_test
async def test_capacity_queue_is_bounded_and_returns_overload():
    redis = fakeredis.aioredis.FakeRedis()
    gate = RedisCapacityGate(
        redis, prefix="test", limit=1, max_queued=0,
        wait_seconds=0.05, lease_ms=1_000,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        started.set()
        await release.wait()
        yield "data: ok\n\n"

    leader = asyncio.create_task(_collect(gate.stream(slow)))
    await started.wait()
    rejected = await _collect(gate.stream(slow))
    release.set()
    await leader

    assert len(rejected) == 1
    assert "AI_CAPACITY_EXCEEDED" in rejected[0]


@async_test
async def test_openai_rpm_and_tpm_budget_is_shared():
    redis = fakeredis.aioredis.FakeRedis()
    first = RedisOpenAIBudget(redis, prefix="test", rpm=2, tpm=100)
    second = RedisOpenAIBudget(redis, prefix="test", rpm=2, tpm=100)

    assert await first.reserve(40) is None
    assert await second.reserve(40) is None
    assert await first.reserve(10) is not None
