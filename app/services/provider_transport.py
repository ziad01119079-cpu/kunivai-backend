"""Reserve budget at the transport boundary for every SDK request."""

import json
import logging
import os

import httpx
from redis.exceptions import RedisError

from .coordination_runtime import get_redis_runtime
from .distributed_coordination import EPOCH_LUA
from .provider_usage import current_usage

logger = logging.getLogger(__name__)

DAILY_LUA = EPOCH_LUA + """
if not epoch_valid(KEYS[2], ARGV[6]) then return -1 end
local day = math.floor(tonumber(redis.call('TIME')[1]) / 86400)
if tonumber(redis.call('HGET', KEYS[1], 'day') or '-1') ~= day then
  redis.call('DEL', KEYS[1])
  redis.call('HSET', KEYS[1], 'day', day)
  redis.call('EXPIRE', KEYS[1], 172800)
end
local fields = {'calls', 'units', 'uploads'}
local charges = {1, tonumber(ARGV[4]), tonumber(ARGV[5])}
for i = 1, 3 do
  local current = tonumber(redis.call('HGET', KEYS[1], fields[i]) or '0')
  if current + charges[i] > tonumber(ARGV[i]) then return 0 end
end
for i = 1, 3 do redis.call('HINCRBY', KEYS[1], fields[i], charges[i]) end
return 1
"""


def charge_request(request: httpx.Request) -> tuple[int, int, int]:
    """Return estimated tokens, reserved hosted tool calls, and upload count."""
    route = request.url.path.rstrip("/")
    if route in ("/v1/responses", "/v1/chat/completions") and request.method == "POST":
        body = json.loads(request.content)
        if body.get("previous_response_id") or body.get("conversation") or body.get("background"):
            raise ValueError("Unaccounted provider context")
        cap = body.get("max_output_tokens") or body.get("max_completion_tokens") or body.get("max_tokens")
        if type(cap) is not int or not 1 <= cap <= 16_384:
            raise ValueError("Missing output reservation")
        tools = body.get("tools") or []
        tool_cap = body.get("max_tool_calls", 0) if tools else 0
        if tools and (
            type(tool_cap) is not int or not 1 <= tool_cap <= 4
            or any(tool.get("type") not in {"file_search", "web_search", "web_search_preview"} for tool in tools)
        ):
            raise ValueError("Unaccounted provider tool")
        context = body.get("input") or body.get("messages") or []
        if not isinstance(context, (list, str)):
            raise ValueError("Unsupported provider input")
        binary = False
        for message in context if isinstance(context, list) else ():
            if not isinstance(message, dict):
                raise ValueError("Unsupported provider input")
            content = message.get("content")
            for part in content if isinstance(content, list) else ():
                if part.get("type") == "input_file" and str(part.get("file_data", "")).startswith("data:application/pdf;base64,"):
                    binary = True
                elif part.get("type") not in {"input_text", "text"}:
                    raise ValueError("Unaccounted provider input")
        tool_tokens = int(os.getenv("OPENAI_TOOL_TOKEN_RESERVATION", "12000"))
        if tool_tokens < 1:
            raise ValueError("Tool reservation must be positive")
        estimate = 128_000 if binary else len(request.content) + 256 + tool_cap * tool_tokens
        return estimate + cap, tool_cap, 0
    if route == "/v1/files" and request.method == "POST":
        return 0, 0, 1
    if route.startswith(("/v1/files/", "/v1/vector_stores/")):
        if request.method in {"GET", "DELETE"} or (
            route.startswith("/v1/vector_stores/") and route.endswith("/files") and request.method == "POST"
        ):
            return 0, 0, 0
    raise ValueError("Unaccounted provider route")


class BudgetedProviderTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport):
        self.transport = transport

    @staticmethod
    def rejection(status: int, code: str, delay: int = 0) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"code": code, "message": "Provider request blocked by application policy."}},
            headers={"Retry-After": str(delay)} if delay else None,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            tokens, tools, uploads = charge_request(request)
        except (ValueError, TypeError, AttributeError):
            logger.warning("provider_reservation_rejected reason=unsupported_request")
            return self.rejection(400, "provider_metering_unsupported")
        runtime = get_redis_runtime()
        try:
            delay = await runtime.openai_budget.reserve(tokens)
            if delay is not None:
                logger.warning("provider_reservation_rejected reason=minute_budget")
                return self.rejection(429, "openai_budget_exhausted", delay)
            units = tokens + tools * int(os.getenv("OPENAI_DAILY_UNITS_PER_TOOL_CALL", "4000"))
            admitted = await runtime.client.eval(
                DAILY_LUA, 2, f"{runtime.openai_budget.prefix}:daily", runtime.epoch_key,
                int(os.getenv("OPENAI_DAILY_REQUEST_BUDGET", "18000")),
                int(os.getenv("OPENAI_DAILY_TOKEN_UNIT_BUDGET", "30000000")),
                int(os.getenv("OPENAI_DAILY_FILE_UPLOAD_BUDGET", "20")),
                units, uploads, runtime.epoch,
            )
            if int(admitted) == -1:
                raise RedisError("Coordination epoch missing or changed")
            if not admitted:
                logger.warning("provider_reservation_rejected reason=daily_budget")
                return self.rejection(429, "openai_daily_budget_exhausted")
        except RedisError:
            logger.warning("provider_reservation_rejected reason=coordination_unavailable")
            return self.rejection(503, "provider_coordination_unavailable")
        trace = current_usage.get()
        logger.info("provider_attempt_reserved", extra={"cost_telemetry": True, "operational_metadata": {
            "trace_id": trace.trace_id if trace else None,
            "reserved_tokens": tokens, "reserved_tool_calls": tools, "reserved_uploads": uploads,
        }})
        return await self.transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self.transport.aclose()
