import asyncio
import json

from app.services.ai_capacity import AICapacityGate


def _payload(event):
    return json.loads(event[6:].strip())


def test_distinct_requests_run_concurrently_up_to_configured_limit():
    gate = AICapacityGate(max_active=2, max_queued=0, queue_timeout=.1)
    entered = 0
    peak = 0

    async def upstream():
        nonlocal entered, peak
        entered += 1
        peak = max(peak, entered)
        await asyncio.sleep(.02)
        yield 'data: {"type":"done"}\n\n'
        entered -= 1

    async def consume():
        return [event async for event in gate.stream(upstream)]

    async def run():
        return await asyncio.gather(consume(), consume())

    results = asyncio.run(run())
    assert peak == 2
    assert all(_payload(items[-1])["type"] == "done" for items in results)


def test_queue_is_bounded_and_overload_is_clear():
    gate = AICapacityGate(max_active=1, max_queued=1, queue_timeout=.2)
    release = asyncio.Event()

    async def upstream():
        await release.wait()
        yield 'data: {"type":"done"}\n\n'

    async def consume():
        return [event async for event in gate.stream(upstream)]

    async def run():
        first = asyncio.create_task(consume())
        await asyncio.sleep(0)
        second = asyncio.create_task(consume())
        await asyncio.sleep(0)
        overloaded = await consume()
        release.set()
        await asyncio.gather(first, second)
        return overloaded

    overloaded = asyncio.run(run())
    payload = _payload(overloaded.single if hasattr(overloaded, "single") else overloaded[0])
    assert payload["code"] == "AI_CAPACITY_EXCEEDED"
    assert payload["retryable"] is True
