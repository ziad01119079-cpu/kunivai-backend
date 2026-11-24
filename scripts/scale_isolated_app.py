"""Zero-egress benchmark adapter for the normal chat pipeline."""

import asyncio
from contextlib import aclosing, asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace as Object
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app import responses_api
from app.auth import verify_token
from app.limiter import limiter
from app.services import chat_service
from app.services.academic_calendar import CalendarComparisonResolution, EvidenceResolution
from app.services.coordination_runtime import close_redis, get_redis_runtime


if (
    os.environ.get("KUNIV_BENCH_ISOLATED") != "1"
    or os.environ.get("REDIS_URL") != "unix:///bench/redis.sock"
    or os.environ.get("REDIS_KEY_PREFIX") != "kuniv-bench"
    or any(os.getenv(key) for key in (
        "OPENAI_API_KEY", "FIREBASE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_APPLICATION_CREDENTIALS", "FIRESTORE_EMULATOR_HOST",
    ))
):
    raise RuntimeError("Benchmark must use the isolated, credential-free socket fixture")

prefix = "kuniv-bench"
message_id = ContextVar("benchmark_message_id")
route = importlib.import_module("app.routers.chat")
lag_ms = []
config = Object(
    OPENAI_MODEL="benchmark-mock", OPENAI_VECTOR_STORE_ID="mock",
    HISTORY_TOKEN_BUDGET=3000,
)


class MockStream:
    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def __aiter__(self):
        key = message_id.get()
        redis = get_redis_runtime().client
        await redis.hincrby(f"{prefix}:calls", key, 1)
        prompt = self.options["input"][-1]["content"]
        profile = UUID(key).int % 100
        first, total, chunks = (
            (1.5, 5, 24) if profile < 65 else
            (2.5, 9, 40) if profile < 90 else (4, 16, 64)
        )
        await asyncio.sleep(first)
        if "[bench:timeout]" in prompt:
            raise TimeoutError()
        if "[bench:rate]" in prompt:
            yield Object(type="error", code="rate_limit_exceeded")
            return
        yield Object(type="response.output_text.delta", delta="إجابة أكاديمية اصطناعية.")
        if "[bench:eof]" in prompt:
            return
        for _ in range(chunks):
            await asyncio.sleep((total - first) / chunks)
            yield Object(type="response.output_text.delta", delta=" تفاصيل تجريبية للسؤال.")
        output = []
        if self.options.get("tools"):
            output.append(Object(type="message", content=[
                Object(type="output_text", annotations=[
                    Object(type="file_citation", file_id="fixture-file", index=0),
                ]),
            ]))
        yield Object(type="response.completed", response=Object(usage=None, output=output))


async def fixture_file(_):
    return Object(filename="mock-calendar.pdf")


async def empty(*_args, **_kwargs):
    return []


async def save_user(uid, session, text, **kwargs):
    key = f"{prefix}:synthetic:user:{uid}:{session}"
    await get_redis_runtime().client.set(key, text, ex=900)
    return kwargs.get("correlation_id")


async def save_assistant(uid, session_id, content, **kwargs):
    key = f"{prefix}:synthetic:assistant:{uid}:{session_id}"
    await get_redis_runtime().client.set(key, content, ex=900)


async def fixture_calendar(_query):
    now = datetime.now(timezone.utc)
    return CalendarComparisonResolution(
        general=EvidenceResolution("unavailable", (), None, now),
        special=(), discovered_special_scopes=(), checked_at=now,
    )


async def traced_chat(**kwargs):
    async with aclosing(chat_service.process_chat(**kwargs)) as stream:
        while True:
            token = message_id.set(kwargs["correlation_id"])
            try:
                item = await anext(stream)
            except StopAsyncIteration:
                return
            finally:
                message_id.reset(token)
            yield item


chat_service.get_config = lambda: config
responses_api.get_config = lambda: config
responses_api.get_openai_client = lambda: Object(
    responses=Object(stream=lambda **options: MockStream(options)),
    files=Object(retrieve=fixture_file),
)
chat_service.load_recent_messages = empty
chat_service.load_user_schedules = empty
chat_service.save_user_message = save_user
chat_service.save_assistant_message = save_assistant
chat_service.lookup_official_calendar = fixture_calendar
chat_service.analyze_and_log_missed_query = empty
route.process_chat = traced_chat


async def synthetic_claim(*_):
    return "claimed"


route.claim_generation = synthetic_claim


async def synthetic_identity(request: Request):
    uid = request.headers.get("x-bench-user", "")
    if request.headers.get("authorization") != "Bearer benchmark-fixture" or not re.fullmatch(r"bench-[0-9-]{1,40}", uid):
        raise HTTPException(401, "Synthetic fixture identity required")
    return uid


async def sample_lag():
    while True:
        started = time.perf_counter()
        await asyncio.sleep(0.02)
        lag_ms.append(max(0, (time.perf_counter() - started - 0.02) * 1000))
        if len(lag_ms) > 3000:
            del lag_ms[:1000]


@asynccontextmanager
async def lifespan(app):
    interfaces = {path.name for path in Path("/sys/class/net").iterdir()}
    if interfaces != {"lo"}:
        raise RuntimeError("Benchmark API must run with Docker network_mode: none")
    await get_redis_runtime().client.ping()
    sample = asyncio.create_task(sample_lag())
    try:
        yield
    finally:
        sample.cancel()
        await asyncio.gather(sample, return_exceptions=True)
        await close_redis()


app = FastAPI(lifespan=lifespan, openapi_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.include_router(route.router)
app.dependency_overrides[verify_token] = synthetic_identity


@app.get("/__mock/stats")
async def stats():
    runtime = get_redis_runtime()
    redis = runtime.client
    started = time.perf_counter()
    await redis.ping()
    ping_ms = (time.perf_counter() - started) * 1000
    info = await redis.info("memory", "clients")
    return {
        "mock": True, "egress": "unix-only", "run_id": os.environ["KUNIV_MOCK_RUN_ID"],
        "worker": os.getpid(), "redis_ping_ms": ping_ms,
        "redis_memory_bytes": info["used_memory"],
        "redis_connected_clients": info["connected_clients"],
        "redis_blocked_clients": info["blocked_clients"],
        "active": await redis.zcard(runtime.capacity.active_key),
        "queued": await redis.zcard(runtime.capacity.queue_key),
        "loop_lag_ms": lag_ms[-300:],
    }


@app.post("/__mock/calls")
async def counts(keys: list[UUID]):
    if len(keys) > 250:
        raise HTTPException(400, "Count batch too large")
    if not keys:
        return []
    raw = await get_redis_runtime().client.hmget(f"{prefix}:calls", [str(key) for key in keys])
    return [int(value or 0) for value in raw]
