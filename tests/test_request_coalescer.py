import asyncio

from app.services.request_coalescer import RequestCoalescer


def test_concurrent_retry_reuses_one_upstream_stream():
    calls = 0
    coalescer = RequestCoalescer(replay_ttl_seconds=10)

    async def upstream():
        nonlocal calls
        calls += 1
        yield 'data: {"type":"status"}\n\n'
        await asyncio.sleep(0.02)
        yield 'data: {"type":"done"}\n\n'

    async def consume():
        return [event async for event in coalescer.stream("same-request", upstream)]

    async def run():
        return await asyncio.gather(consume(), consume())

    first, retry = asyncio.run(run())
    assert calls == 1
    assert retry == first


def test_completed_retry_replays_without_new_upstream_call():
    calls = 0
    coalescer = RequestCoalescer(replay_ttl_seconds=10)

    async def upstream():
        nonlocal calls
        calls += 1
        yield 'data: {"type":"done"}\n\n'

    async def run():
        first = [event async for event in coalescer.stream("same-request", upstream)]
        retry = [event async for event in coalescer.stream("same-request", upstream)]
        return first, retry

    first, retry = asyncio.run(run())
    assert calls == 1
    assert retry == first


def test_replay_buffer_is_bounded_without_interrupting_original_stream():
    coalescer = RequestCoalescer(max_replay_bytes=10)

    async def upstream():
        yield 'data: {"type":"chunk","content":"long answer"}\n\n'
        yield 'data: {"type":"done"}\n\n'

    async def run():
        original = [event async for event in coalescer.stream("request", upstream)]
        retry = [event async for event in coalescer.stream("request", upstream)]
        return original, retry

    original, retry = asyncio.run(run())
    assert len(original) == 2
    assert "REPLAY_BUFFER_LIMIT" in retry[0]
