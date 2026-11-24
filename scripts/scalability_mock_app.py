"""Isolated loopback-only fixture. Uses the real chat route and Redis runtime."""

import asyncio
import json
import os
import sqlite3
import time
from uuid import UUID
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.routers import chat_router
from app.auth import verify_token
from app.limiter import limiter
from app.services.coordination_runtime import close_redis, get_redis_runtime
import importlib

chat_module = importlib.import_module("app.routers.chat")

if os.environ.get("KUNIV_LOCAL_MOCK") != "1":
    raise RuntimeError("This fixture requires KUNIV_LOCAL_MOCK=1")

lag_samples = []


async def measure_lag():
    while True:
        started = time.perf_counter()
        await asyncio.sleep(0.02)
        lag_samples.append((time.perf_counter() - started - 0.02) * 1000)
        if len(lag_samples) > 3000:
            del lag_samples[:1000]


@asynccontextmanager
async def lifespan(app):
    await get_redis_runtime().client.ping()
    task = asyncio.create_task(measure_lag())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await close_redis()


async def mock_provider(*, correlation_id, message, **kwargs):
    runtime = get_redis_runtime()
    prefix = os.environ["REDIS_KEY_PREFIX"]
    count = await runtime.client.hincrby(f"{prefix}:mock:calls", correlation_id, 1)
    if os.getenv("KUNIV_MOCK_PROFILE") == "student-mix":
        percentile = UUID(correlation_id).int % 100
        first, total, chunks = (
            (1.5, 5, 24) if percentile < 65 else
            (2.5, 9, 40) if percentile < 90 else (4, 16, 64)
        )
        await asyncio.sleep(first)
        yield 'data: {"type":"chunk","content":"إجابة تجريبية للطالب"}\n\n'
        step = (total - first) / chunks
        for _ in range(chunks):
            await asyncio.sleep(step)
            yield 'data: {"type":"chunk","content":" نص إضافي من الإجابة التجريبية."}\n\n'
        yield 'data: {"type":"done"}\n\n'
        return
    await asyncio.sleep(0.05)
    yield "data: " + json.dumps({"type": "chunk", "content": "first"}) + "\n\n"
    if message == "kill-owner" and count == 1:
        await asyncio.sleep(30)
    await asyncio.sleep(float(os.getenv("MOCK_TOKEN_INTERVAL", "0.005")))
    for _ in range(8):
        yield 'data: {"type":"chunk","content":" mock"}\n\n'
        await asyncio.sleep(float(os.getenv("MOCK_TOKEN_INTERVAL", "0.005")))
    yield 'data: {"type":"done"}\n\n'


async def mock_identity(request: Request):
    return request.headers.get("x-mock-user", "test-user")


def durable_claim(uid, session, key, token, fingerprint):
    with sqlite3.connect(os.environ["KUNIV_MOCK_CLAIMS_DB"], timeout=5) as db:
        inserted = db.execute(
            "INSERT OR IGNORE INTO claims VALUES (?, ?, ?, ?)", (uid, session, key, fingerprint),
        ).rowcount
        if inserted:
            return "claimed"
        existing = db.execute(
            "SELECT fingerprint FROM claims WHERE uid=? AND session=? AND message=?", (uid, session, key),
        ).fetchone()
        return "existing" if existing[0] == fingerprint else "conflict"


async def mock_durable_claim(*args):
    return await asyncio.to_thread(durable_claim, *args)


chat_module.claim_generation = mock_durable_claim
chat_module.process_chat = mock_provider
app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.include_router(chat_router)
app.dependency_overrides[verify_token] = mock_identity


@app.middleware("http")
async def identify_worker(request, call_next):
    response = await call_next(request)
    response.headers["X-Test-Worker"] = str(os.getpid())
    return response


@app.get("/__mock/stats")
async def stats():
    return {"mock": True, "run_id": os.environ.get("KUNIV_MOCK_RUN_ID"),
            "lag_ms": lag_samples[-2500:]}


class TraceChat:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", ()))
        request_id = headers.get(b"x-request-id")
        if scope["type"] != "http" or scope["path"] != "/chat" or not request_id:
            await self.app(scope, receive, send)
            return
        identity = {"request_id": request_id.decode("ascii"), "worker": os.getpid()}
        print(json.dumps({**identity, "phase": "server_entry"}), flush=True)

        async def traced_send(message):
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                print(json.dumps({**identity, "phase": "server_eof"}), flush=True)

        try:
            await self.app(scope, receive, traced_send)
        except Exception as exc:
            print(json.dumps({
                **identity,
                "server_error": "".join(traceback.format_exception(exc))[-10000:],
            }), flush=True)
            raise


app.add_middleware(TraceChat)
