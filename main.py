"""
main.py — FastAPI application entry point for Railway deployment.

Endpoints:
  POST   /upload              Upload file to OpenAI Vector Store
  GET    /assets              List files in Vector Store
  DELETE /assets/{file_id}   Delete file from Vector Store
  POST   /chat                SSE streaming chat (Responses API + file_search)
  POST   /schedules/import    Extract and store a student schedule PDF
  GET    /health              Health check

All endpoints (except /health) require:
  Authorization: Bearer <Firebase ID Token>

Environment variables (set on Railway → Variables):
  OPENAI_API_KEY
  OPENAI_VECTOR_STORE_ID
  FIREBASE_SERVICE_ACCOUNT_JSON
  OPENAI_MODEL               (optional, default: gpt-4o)
  OPENAI_SCHEDULE_MODEL      (optional, defaults to OPENAI_MODEL)
  HISTORY_TOKEN_BUDGET       (optional, default: 3000)
  PORT                       (set automatically by Railway)
"""

import logging
import os
import hmac

from dotenv import load_dotenv
from app.local_notification_test import enabled as local_notification_test_enabled

# Load .env for local development (no-op in production where env vars are set directly)
if not local_notification_test_enabled():
    load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from contextlib import asynccontextmanager
import uuid

from app.firebase import init_firebase
from app.limiter import limiter
from app.routers import (
    admin_users_router,
    assets_router,
    chat_router,
    notifications_router,
    schedules_router,
    upload_router,
)
from app.logger import setup_logging, request_id_var
from app.task_tracker import background_tasks
from app.services.coordination_runtime import close_redis, distributed_enabled, get_redis_runtime, redis_ready
from app.openai_client import close_openai_client


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
setup_logging()
logger = logging.getLogger(__name__)

# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting KuwaitChatAI backend…")
    
    # Fail-fast validation
    from app.config import get_config
    try:
        cfg = get_config()
        logger.info(
            "Config OK — model=%s  schedule_model=%s  history_budget=%d",
            cfg.OPENAI_MODEL,
            cfg.OPENAI_SCHEDULE_MODEL,
            cfg.HISTORY_TOKEN_BUDGET,
        )
        init_firebase()
        if os.getenv("KUNIV_SCALABLE", "0") == "1":
            if not distributed_enabled() or not get_redis_runtime().epoch or "*" in cors_origins:
                raise RuntimeError("Scalable mode requires Redis epoch and explicit CORS origins")
        if distributed_enabled() and not await redis_ready():
            raise RuntimeError("Redis coordination is enabled but unavailable")
    except Exception as e:
        logger.error("Startup validation failed (%s)", type(e).__name__)
        raise RuntimeError("Startup validation failed") from None

    # Notification recovery is deliberately outside fail-fast dependency
    # validation. Firestore/OpenAI recovery outages must not take chat/RAG down.
    try:
        from app.services.knowledge_file_notifications import (
            recover_pending_knowledge_notification_jobs,
        )

        background_tasks.create_task(recover_pending_knowledge_notification_jobs())
    except Exception:
        logger.exception(
            "Could not schedule knowledge-job recovery; core API startup continues"
        )
        
    yield  # App runs here
    
    logger.info("Shutting down KuwaitChatAI backend…")
    await background_tasks.wait_all(timeout=10.0)
    try:
        await close_redis()
    finally:
        await close_openai_client()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="KuwaitChatAI Backend",
    description="RAG backend using OpenAI Responses API + File Search",
    version="2.0.0",
    docs_url="/docs",      # Swagger UI (disable in prod if desired)
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Rate Limiting ───────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ────────────────────────────────────────────────────────────────────
# Allow requests from Flutter Web, mobile, and the admin dashboard.
cors_origins = [
    origin.strip() for origin in os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Client-Message-ID", "X-Request-ID", "Retry-After"],
)

# ── Middleware ──────────────────────────────────────────────────────────────
class CorrelationIdMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        req_id_bytes = headers.get(b"x-request-id")
        req_id = str(uuid.uuid4())
        if req_id_bytes and len(req_id_bytes) <= 64:
            try:
                req_id = str(uuid.UUID(req_id_bytes.decode("ascii")))
            except (ValueError, UnicodeError):
                pass
            
        token = request_id_var.set(req_id)
        
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                res_headers = message.setdefault("headers", [])
                if not any(k.lower() == b"x-request-id" for k, v in res_headers):
                    res_headers.append((b"x-request-id", req_id.encode("latin1")))
            await send(message)
            
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)

app.add_middleware(CorrelationIdMiddleware)

# ── Routes ──────────────────────────────────────────────────────────────────
app.include_router(upload_router)
app.include_router(admin_users_router)
app.include_router(assets_router)
app.include_router(chat_router)
app.include_router(schedules_router)
app.include_router(notifications_router)

@app.get("/health", tags=["health"])
async def health():
    """Liveness check for container orchestrators."""
    return {"status": "alive"}

@app.get("/ready", tags=["health"])
async def ready():
    """Readiness check validating config and dependencies."""
    try:
        from app.config import get_config
        get_config()
        # Verify Firebase is initialized
        from firebase_admin import get_app
        get_app()
        if not await redis_ready():
            raise RuntimeError("Redis coordination unavailable")
        return {"status": "ready"}
    except Exception as e:
        logger.error("Readiness check failed (%s)", type(e).__name__)
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Service not ready")


@app.get("/internal/capacity", include_in_schema=False)
async def capacity_metrics(request: Request):
    token = os.getenv("KUNIV_METRICS_TOKEN")
    supplied = request.headers.get("authorization", "")
    if not token or not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        raise HTTPException(404)
    if not distributed_enabled() or not await redis_ready():
        raise HTTPException(503, "Redis coordination unavailable")
    runtime = get_redis_runtime()
    try:
        seconds, micros = await runtime.client.time()
        now = int(seconds) * 1000 + int(micros) // 1000
        async with runtime.client.pipeline(transaction=False) as pipe:
            pipe.zcount(runtime.capacity.active_key, now, "+inf")
            pipe.zcount(runtime.capacity.queue_key, now, "+inf")
            pipe.zrangebyscore(
                runtime.capacity.queue_key, now, "+inf", start=0, num=1, withscores=True,
            )
            pipe.zcount(f"{runtime.requests.prefix}:sse", now, "+inf")
            pipe.info("clients")
            active, queued, oldest, subscribers, clients = await pipe.execute()
    except Exception:
        raise HTTPException(503, "Coordination metrics unavailable") from None
    values = {
        "kuniv_generations_active": active, "kuniv_queue_depth": queued,
        "kuniv_queue_oldest_wait_seconds": (
            max(0.0, runtime.capacity.wait_seconds - (oldest[0][1] - now) / 1000) if oldest else 0.0
        ),
        "kuniv_sse_subscribers": subscribers,
        "kuniv_redis_connected_clients": clients["connected_clients"],
        "kuniv_redis_blocked_clients": clients["blocked_clients"],
    }
    return PlainTextResponse("".join(f"{name} {value}\n" for name, value in values.items()))
