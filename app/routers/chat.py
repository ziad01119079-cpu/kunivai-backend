"""
routers/chat.py

POST /chat
"""

import logging
import os
import re
import asyncio
import hashlib
import json
import uuid
from contextlib import aclosing

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from redis.exceptions import RedisError

from ..auth import verify_chat_app_check, verify_token
from ..chat_repository import claim_generation
from ..services.chat_service import process_chat
from ..services.request_coalescer import chat_request_coalescer
from ..services.ai_capacity import ai_capacity_gate
from ..services.user_rate_limiter import chat_user_rate_limiter, UserRateLimitExceeded
from ..services.coordination_runtime import distributed_enabled, get_redis_runtime
from ..services.distributed_coordination import RedisCoordinator
from ..limiter import limiter

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=2000, description="User message content")
    sessionId: str = Field(..., max_length=128, description="Unique session ID")
    systemContext: str | None = Field(None, max_length=1000, description="Optional extra system instructions")
    client_message_id: str | None = Field(None, description="Client-generated correlation ID")

    @field_validator("client_message_id")
    def validate_uuid(cls, v):
        if v is not None:
            try:
                return str(uuid.UUID(v))
            except ValueError:
                raise ValueError("client_message_id must be a valid UUID string")
        return v



@router.post("/chat")
@limiter.limit(os.getenv("AI_IP_RATE_LIMIT", "120/minute"), exempt_when=distributed_enabled)
async def chat(
    request: Request,
    body: ChatRequest,
    uid: str = Depends(verify_token),
):
    """
    Stream an AI response to the client as Server-Sent Events.
    """
    message = body.message.strip()
    session_id = body.sessionId.strip()

    if not message:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'message' cannot be empty.",
        )
    if not session_id or re.search(r"[/:\x00-\x1f\x7f]", session_id) or session_id in {".", ".."}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid sessionId.",
        )

    distributed = distributed_enabled()
    cursor = request.headers.get("last-event-id", "0-0")
    if distributed:
        if not re.fullmatch(r"[0-9]{1,20}-[0-9]{1,20}", cursor) or any(
            int(part) >= 2**64 for part in cursor.split("-")
        ):
            raise HTTPException(400, "Invalid Last-Event-ID")
        if cursor != "0-0" and body.client_message_id is None:
            raise HTTPException(422, "client_message_id is required to resume")
    await verify_chat_app_check(request.headers.get("x-firebase-appcheck"))
    message_id = body.client_message_id or str(uuid.uuid4())
    rejection = None
    try:
        if distributed:
            runtime = get_redis_runtime()
            rejection = await runtime.admission.reserve(uid)
        else:
            await chat_user_rate_limiter.check(uid)
    except UserRateLimitExceeded as exc:
        rejection = ("AI_USER_RATE_LIMITED", exc.retry_after)
    except RedisError:
        logger.warning("Distributed chat admission unavailable")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "2"},
            content={"code": "AI_COORDINATION_UNAVAILABLE", "retryable": True,
                     "message": "الخدمة غير متاحة مؤقتاً. حاول مرة أخرى بعد قليل."},
        )
    if rejection is not None:
        code, retry_after = rejection
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
            content={
                "code": code,
                "retryable": True,
                "retry_after_seconds": retry_after,
                "message": "الخدمة مشغولة مؤقتًا. حاول مرة أخرى بعد قليل.",
            },
        )

    # Use the service layer to process the chat
    def generation_factory(**kwargs):
        return process_chat(
            uid=uid, session_id=session_id, message=message,
            system_context=body.systemContext, correlation_id=message_id, **kwargs,
        )

    if distributed:
        request_key = f"{uid}:{session_id}:{message_id}"
        fingerprint = hashlib.sha256(
            json.dumps([message, body.systemContext], ensure_ascii=False).encode()
        ).hexdigest()

        async def authorized_generation():
            try:
                async with asyncio.timeout(float(os.getenv("AI_GENERATION_TIMEOUT_SECONDS", "120"))):
                    token = uuid.uuid4().hex
                    result = await claim_generation(uid, session_id, message_id, token, fingerprint)
                    if result == "conflict":
                        yield RedisCoordinator._error("MESSAGE_ID_CONFLICT", "معرّف الرسالة مستخدم لمحتوى مختلف.", retryable=False)
                        return
                    if result != "claimed":
                        yield RedisCoordinator._error(
                            "GENERATION_OUTCOME_UNKNOWN", "تحقق من سجل المحادثة قبل إرسال رسالة جديدة.", retryable=False,
                        )
                        return
                    async with aclosing(generation_factory(generation_claim=token)) as events:
                        async for event in events:
                            yield event
            except TimeoutError:
                yield RedisCoordinator._error("AI_GENERATION_TIMEOUT", "تعذر إكمال الإجابة في الوقت المحدد.", retryable=False)

        stream = runtime.subscribers.stream(
            uid, lambda: runtime.requests.stream(
                request_key, lambda: runtime.capacity.stream(authorized_generation, uid=uid),
                last_event_id=cursor, fingerprint=fingerprint,
            ),
        )
    else:
        stream = chat_request_coalescer.stream(
            f"{uid}:{session_id}:{message_id}", lambda: ai_capacity_gate.stream(generation_factory),
        )

    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # Nginx: disable buffering
            "X-Client-Message-ID": message_id,
        },
    )
