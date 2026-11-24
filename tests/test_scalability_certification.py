import asyncio
import importlib
from types import SimpleNamespace
from uuid import uuid4

import fakeredis.aioredis
import httpx
from fastapi import FastAPI

from app.auth import verify_chat_app_check, verify_token
from app.services.distributed_coordination import (
    RedisCapacityGate, RedisCoordinator, RedisOpenAIBudget, RedisSubscriberGate, RedisUserRateLimiter,
)
from app.services.provider_transport import BudgetedProviderTransport

route = importlib.import_module("app.routers.chat")


def test_capacity_metrics_require_token_and_report_queue_age(monkeypatch):
    import main

    class MetricsRedis(fakeredis.aioredis.FakeRedis):
        def pipeline(self, *args, **kwargs):
            pipe = super().pipeline(*args, **kwargs)
            pipe.info = lambda _: pipe.hgetall("test:client-info")
            return pipe

    async def run():
        async with MetricsRedis(decode_responses=True) as redis:
            capacity = RedisCapacityGate(redis, prefix="test", limit=2, max_queued=2, wait_seconds=10, lease_ms=1000)
            runtime = SimpleNamespace(client=redis, capacity=capacity, requests=SimpleNamespace(prefix="test"))

            async def ready():
                return True

            monkeypatch.setenv("COORDINATION_BACKEND", "redis")
            monkeypatch.setenv("KUNIV_METRICS_TOKEN", "local-test-credential")
            monkeypatch.setattr(main, "redis_ready", ready)
            monkeypatch.setattr(main, "get_redis_runtime", lambda: runtime)
            seconds, micros = await redis.time()
            now = seconds * 1000 + micros // 1000

            async def sample_time():
                return seconds, micros

            monkeypatch.setattr(redis, "time", sample_time)
            await redis.zadd(capacity.queue_key, {"expired": now - 5, "oldest": now + 4000, "newer": now + 7000})
            await redis.hset("test:client-info", mapping={"connected_clients": 3, "blocked_clients": 1})
            async with httpx.AsyncClient(base_url="http://test", transport=httpx.ASGITransport(app=main.app)) as client:
                assert (await client.get("/internal/capacity")).status_code == 404
                response = await client.get("/internal/capacity", headers={"Authorization": "Bearer local-test-credential"})
                assert response.status_code == 200
                values = dict(line.split() for line in response.text.splitlines())
                assert values["kuniv_queue_depth"] == "2"
                assert values["kuniv_queue_oldest_wait_seconds"] == "6.0"

    asyncio.run(run())


def test_chat_identity_fairness_and_loss_does_not_regenerate(monkeypatch):
    async def run():
        redis = fakeredis.aioredis.FakeRedis()
        runtime = SimpleNamespace(
            client=redis, epoch="", epoch_key="test:epoch",
            users=RedisUserRateLimiter(redis, prefix="test", requests=30, window_seconds=60),
            requests=RedisCoordinator(redis, prefix="test"),
            capacity=RedisCapacityGate(redis, prefix="test", limit=2, max_queued=2, wait_seconds=1, lease_ms=1000),
            subscribers=RedisSubscriberGate(redis, prefix="test", limit=8, per_user=4, local_limit=8, seconds=5),
            starts=RedisOpenAIBudget(redis, prefix="test:starts", rpm=2, tpm=1),
        )
        durable = {}
        calls = []

        async def claim(uid, session, message_id, token, fingerprint):
            key = (uid, session, message_id)
            if key in durable:
                return "existing" if durable[key] == fingerprint else "conflict"
            durable[key] = fingerprint
            return "claimed"

        async def generation(**kwargs):
            calls.append(kwargs)
            await asyncio.sleep(.02)
            yield 'data: {"type":"done"}\n\n'

        first = FastAPI()
        second = FastAPI()
        for app, uid in ((first, "u1"), (second, "u2")):
            async def identity(value=uid):
                return value
            app.include_router(route.router)
            app.dependency_overrides[verify_token] = identity
        monkeypatch.setenv("COORDINATION_BACKEND", "redis")
        monkeypatch.setattr(route, "get_redis_runtime", lambda: runtime)
        monkeypatch.setattr(route, "process_chat", generation)
        monkeypatch.setattr(route, "claim_generation", claim)
        body = {"sessionId": "session", "message": "question"}
        async with (
            httpx.AsyncClient(base_url="http://test", transport=httpx.ASGITransport(app=first)) as a,
            httpx.AsyncClient(base_url="http://test", transport=httpx.ASGITransport(app=second)) as b,
        ):
            assert (await a.post("/chat", json=body)).status_code == 422
            assert (await a.post("/chat", json={**body, "client_message_id": "invalid"})).status_code == 422
            body["client_message_id"] = str(uuid4())
            one, replay = await asyncio.gather(*(a.post("/chat", json=body) for _ in range(2)))
            assert one.text == replay.text and '"done"' in one.text
            assert "MESSAGE_ID_CONFLICT" in (await a.post("/chat", json={**body, "message": "other"})).text
            assert '"done"' in (await b.post("/chat", json=body)).text
            assert "AI_GLOBAL_RATE_LIMITED" in (await a.post("/chat", json={**body, "client_message_id": str(uuid4())})).text
            # Simulate complete Redis loss. The separate durable record survives.
            await redis.flushdb()
            assert "GENERATION_OUTCOME_UNKNOWN" in (await a.post("/chat", json=body)).text
            assert len(calls) == 2 and all(c.get("generation_claim") for c in calls)
        await redis.aclose()

    asyncio.run(run())


def test_subscriber_admission_releases_on_cancellation():
    async def run():
        redis = fakeredis.aioredis.FakeRedis()
        options = {"prefix": "s", "limit": 2, "per_user": 1, "local_limit": 2, "seconds": 5}
        left, right = RedisSubscriberGate(redis, **options), RedisSubscriberGate(redis, **options)
        entered = asyncio.Event()

        async def waiting():
            entered.set()
            await asyncio.Event().wait()
            yield "unused"

        task = asyncio.create_task(anext(left.stream("u1", waiting)))
        await entered.wait()
        assert "SSE_CAPACITY_EXCEEDED" in await anext(right.stream("u1", waiting))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert await redis.zcard("s:sse") == 0
        await redis.aclose()

    asyncio.run(run())


def test_provider_transport_accounts_failed_attempts_and_secondary_routes(monkeypatch):
    async def run():
        redis = fakeredis.aioredis.FakeRedis()
        monkeypatch.setenv("OPENAI_DAILY_REQUEST_BUDGET", "2")
        runtime = SimpleNamespace(
            client=redis, epoch="", epoch_key="p:epoch",
            openai_budget=RedisOpenAIBudget(redis, prefix="p", rpm=10, tpm=450000),
        )
        monkeypatch.setattr("app.services.provider_transport.get_redis_runtime", lambda: runtime)
        sent = []

        def upstream(request):
            sent.append(request.url.path)
            return httpx.Response(500 if len(sent) == 1 else 200, json={})

        source = BudgetedProviderTransport(httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=source, base_url="https://mock.invalid") as client:
            body = {"model": "gpt-4o", "input": [{"role": "user", "content": "امتحان"}], "max_output_tokens": 100}
            assert (await client.post("/v1/responses", json=body)).status_code == 500
            assert (await client.get("/v1/files/file-unit")).status_code == 200
            denied = await client.post("/v1/responses", json=body)
            assert denied.status_code == 429
            assert (await client.post("/v1/batches", json={})).status_code == 400
            assert sent == ["/v1/responses", "/v1/files/file-unit"]
            assert int(await redis.hget("p:daily", "calls")) == 2
        runtime.openai_budget.epoch = "required"
        async with httpx.AsyncClient(
            transport=BudgetedProviderTransport(httpx.MockTransport(upstream)), base_url="https://mock.invalid",
        ) as client:
            assert (await client.get("/v1/files/file-unit")).status_code == 503
        await redis.aclose()

    asyncio.run(run())


def test_app_check_enforces_app_allowlist(monkeypatch):
    import pytest
    from fastapi import HTTPException

    monkeypatch.setenv("CHAT_APP_CHECK_MODE", "enforce")
    monkeypatch.setenv("CHAT_APP_CHECK_APP_IDS", "allowed-id")
    monkeypatch.setattr("app.auth.app_check.verify_token", lambda value: {"sub": value})
    asyncio.run(verify_chat_app_check("allowed-id"))
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(verify_chat_app_check("some-other-id"))
    assert rejected.value.status_code == 403
