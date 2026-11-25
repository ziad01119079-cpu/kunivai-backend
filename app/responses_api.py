"""
app/responses_api.py

Calls the OpenAI Responses API with file_search and streams SSE events.

SSE event format (matches Flutter parser):
  data: {"type": "chunk",  "content": "<text>"}   — per token
  data: {"type": "done",   "citations": [...]}     — end of stream
  data: {"type": "error",  "message": "<reason>"}  — on failure

FileCitation fields (Responses API, Python SDK):
  annotation.file_id   — OpenAI file ID
  annotation.index     — character index in response text
  (filename is fetched separately from the Files API after streaming)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import AsyncIterator

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

from .config import get_config
from .openai_client import get_openai_client
from .services.coordination_runtime import distributed_enabled
from .services.response_policy import has_previous_answer, response_policy_for
from .services.provider_usage import ProviderAttempt, input_breakdown

logger = logging.getLogger(__name__)

_TRANSIENT_STREAM_CODES = {"server_error", "internal_error"}
_KNOWN_PROVIDER_CODES = _TRANSIENT_STREAM_CODES | {
    "rate_limit_exceeded", "insufficient_quota", "invalid_api_key",
    "invalid_request_error", "model_not_found", "vector_store_not_found",
    "openai_budget_exhausted", "openai_daily_budget_exhausted", "provider_coordination_unavailable",
}


class _RetryBeforeOutput(Exception):
    pass


def _provider_code(error) -> str:
    if isinstance(error, Mapping):
        error = error.get("error", error)
    code = error.get("code") if isinstance(error, Mapping) else getattr(error, "code", None)
    return code if isinstance(code, str) and code in _KNOWN_PROVIDER_CODES else "unknown"


def _provider_request_id(headers) -> str | None:
    value = headers.get("x-request-id") if headers is not None else None
    return value if isinstance(value, str) and re.fullmatch(r"req_[A-Za-z0-9_-]{1,80}", value) else None


def _log_provider_failure(code, request_id, *, has_output, status=None):
    logger.warning(
        "Responses API failure code=%s provider_request_id=%s status=%s has_output=%s",
        code, request_id or "unavailable", status, has_output,
    )


def _provider_error_event(code="LLM_UNAVAILABLE", *, retryable=True) -> str:
    return "data: " + json.dumps({
        "type": "error", "code": code, "retryable": retryable,
        "message": "تعذر إكمال الإجابة من مزود الذكاء الاصطناعي.",
    }) + "\n\n"


@dataclass
class Citation:
    file_id: str
    filename: str
    index: int


@dataclass
class StreamResult:
    full_text: str
    citations: list[Citation]


async def stream_response(
    messages: list[dict],   # [{"role": "user"|"assistant", "content": "..."}]
    system_prompt: str,
    tools: list[dict] | None = None,
    max_output_tokens: int | None = None,
) -> AsyncIterator[str]:
    """Retry one known transient in-band provider failure before any text."""
    retry_reason = None
    for attempt in range(2):
        try:
            async with aclosing(_stream_response_once(
                messages, system_prompt, tools, max_output_tokens, allow_retry=attempt == 0, retry_reason=retry_reason,
            )) as stream:
                async for item in stream:
                    yield item
            return
        except _RetryBeforeOutput as exc:
            retry_reason = str(exc)
            logger.info("Retrying Responses API once after a transient in-stream failure")
            await asyncio.sleep(0.3)


async def _stream_response_once(
    messages: list[dict],
    system_prompt: str,
    tools: list[dict] | None,
    max_output_tokens: int | None,
    *,
    allow_retry: bool,
    retry_reason: str | None,
) -> AsyncIterator[str]:
    """
    Async generator that yields SSE-formatted strings.

    Yields:
        SSE lines like `data: {...}\\n\\n`
    After the generator is exhausted the caller can read `.result`
    by awaiting the returned coroutine.

    NOTE: The generator accumulates the full text and citations internally,
    and yields a final "done" event with citations after the stream closes.
    """
    client = get_openai_client()
    config = get_config()

    input_messages = [
        {"role": "system", "content": system_prompt},
        *messages,
    ]
    if max_output_tokens is None:
        latest = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None)
        question = str(messages[latest].get("content", "")) if latest is not None else ""
        prior = messages[:latest] if latest is not None else []
        policy = response_policy_for(question, previous_answer=has_previous_answer(prior))
        # Retrieval and citations can need room even when the visible answer
        # is short. These ceilings guard usage; the prompt sets the answer length.
        max_output_tokens = max(policy.max_output_tokens, 850 if tools is None or tools else 0)

    full_text_parts: list[str] = []
    raw_citations: list[dict] = []   # file and URL citations
    evidence_texts: list[str] = []
    usage: dict | None = None
    completed = False
    incomplete_reason: str | None = None
    provider_request_id = None
    stream_opened = False
    if tools is None:
        tools = [{"type": "file_search", "vector_store_ids": [config.OPENAI_VECTOR_STORE_ID]}]
    telemetry = ProviderAttempt(
        config.OPENAI_MODEL,
        "responses",
        input_breakdown(messages, system_prompt, config.OPENAI_MODEL),
        tuple(tool["type"] for tool in tools),
        retry_reason,
    )

    try:
        logger.info("Starting Responses API stream")

        request_options = {
            "model": config.OPENAI_MODEL,
            "input": input_messages,
            "tools": tools,
        }
        request_options["max_output_tokens"] = max_output_tokens
        if tools:
            request_options["tool_choice"] = "required"
            if distributed_enabled():
                request_options["max_tool_calls"] = 2
        if any(tool.get("type") == "file_search" for tool in tools):
            # `include=None` is not equivalent to omission for every SDK/API
            # version, so send this parameter only for File Search requests.
            request_options["include"] = ["file_search_call.results"]

        # Responses API streaming — async for loop over events
        async with client.responses.stream(
            **request_options,
        ) as stream:
            stream_opened = True
            # The SDK's stream exposes the HTTP response via a private field.
            # This optional diagnostic must remain safe if that field moves.
            provider_request_id = _provider_request_id(
                getattr(getattr(stream, "_response", None), "headers", None)
            )
            async for event in stream:
                event_type = event.type
                telemetry.observe_tool(getattr(event, "item", None))
                response = getattr(event, "response", None)
                if response is not None:
                    telemetry.response = response
                    telemetry.terminal_response = event_type in {"response.completed", "response.incomplete", "response.failed"}

                # ── Text delta ──────────────────────────────────────────
                if event_type == "response.output_text.delta":
                    delta: str = event.delta or ""
                    if delta:
                        full_text_parts.append(delta)
                        yield (
                            "data: "
                            + json.dumps({"type": "chunk", "content": delta})
                            + "\n\n"
                        )

                # ── Completed: collect file_search annotations ──────────
                elif event_type == "response.completed":
                    completed = True
                    telemetry.outcome = "completed"
                    response = event.response
                    response_usage = getattr(response, "usage", None)
                    if response_usage is not None:
                        usage = {
                            "prompt_tokens": getattr(response_usage, "input_tokens", 0),
                            "completion_tokens": getattr(response_usage, "output_tokens", 0),
                            "total_tokens": getattr(response_usage, "total_tokens", 0),
                        }
                    for output in getattr(response, "output", None) or []:
                        if getattr(output, "type", None) == "file_search_call":
                            for result in getattr(output, "results", None) or []:
                                text = str(getattr(result, "text", "") or "").strip()
                                if text:
                                    evidence_texts.append(text)
                        if getattr(output, "type", None) == "message":
                            for part in getattr(output, "content", None) or []:
                                if getattr(part, "type", None) == "output_text":
                                    for ann in (
                                        getattr(part, "annotations", None) or []
                                    ):
                                        if (
                                            getattr(ann, "type", None)
                                            == "file_citation"
                                        ):
                                            raw_citations.append(
                                                {
                                                    "file_id": ann.file_id,
                                                    "index": ann.index,
                                                    "filename": getattr(ann, "filename", None),
                                                }
                                            )
                                        elif (
                                            getattr(ann, "type", None)
                                            == "url_citation"
                                        ):
                                            raw_citations.append(
                                                {
                                                    "type": "url_citation",
                                                    "url": ann.url,
                                                    "title": getattr(ann, "title", ann.url),
                                                    "start_index": getattr(ann, "start_index", 0),
                                                    "end_index": getattr(ann, "end_index", 0),
                                                }
                                            )

                elif event_type == "response.incomplete":
                    response = event.response
                    telemetry.outcome = "incomplete"
                    details = getattr(response, "incomplete_details", None)
                    incomplete_reason = str(
                        getattr(details, "reason", None) or "unknown"
                    )
                elif event_type in {"error", "response.failed"}:
                    error = getattr(event.response, "error", None) if event_type == "response.failed" else event
                    code = _provider_code(error)
                    telemetry.outcome = code
                    _log_provider_failure(code, provider_request_id, has_output=bool(full_text_parts))
                    if allow_retry and not full_text_parts and code in _TRANSIENT_STREAM_CODES:
                        raise _RetryBeforeOutput(code)
                    yield _provider_error_event(
                        "LLM_RATE_LIMITED" if code == "rate_limit_exceeded" else "LLM_UNAVAILABLE",
                        retryable=code in _TRANSIENT_STREAM_CODES or code == "rate_limit_exceeded",
                    )
                    return

    except _RetryBeforeOutput:
        raise
    except (TimeoutError, APITimeoutError):
        telemetry.outcome = "timeout"
        _log_provider_failure("timeout", provider_request_id, has_output=bool(full_text_parts))
        yield (
            "data: "
            + json.dumps({
                "type": "error", "code": "LLM_TIMEOUT", "retryable": True,
                "message": "انتهت مهلة مزود الذكاء الاصطناعي. حاول مرة أخرى.",
            })
            + "\n\n"
        )
        return
    except APIError as exc:
        code = _provider_code(exc.body)
        if code == "unknown":
            code = _provider_code(exc)
        telemetry.outcome = code
        status = exc.status_code if isinstance(exc, APIStatusError) else None
        request_id = provider_request_id or _provider_request_id(
            exc.response.headers if isinstance(exc, APIStatusError) else None
        )
        _log_provider_failure(code, request_id, has_output=bool(full_text_parts), status=status)
        if isinstance(exc, APIStatusError) and code == "openai_budget_exhausted":
            yield "data: " + json.dumps({
                "type": "error", "code": "OPENAI_BUDGET_EXHAUSTED",
                "retryable": True, "retry_after_seconds": int(exc.response.headers.get("retry-after", "1")),
                "message": "تم بلوغ حد مزود الذكاء الاصطناعي مؤقتاً. حاول بعد قليل.",
            }) + "\n\n"
            return
        if (
            stream_opened and status is None and code in _TRANSIENT_STREAM_CODES
            and allow_retry and not full_text_parts
        ):
            raise _RetryBeforeOutput(code) from None
        temporarily_limited = code == "rate_limit_exceeded" or (
            status == 429 and code != "insufficient_quota"
        )
        yield _provider_error_event(
            "LLM_RATE_LIMITED" if temporarily_limited else "LLM_UNAVAILABLE",
            retryable=(
                temporarily_limited or code in _TRANSIENT_STREAM_CODES
                or isinstance(exc, APIConnectionError)
                or (status is not None and (status == 408 or status >= 500))
            ),
        )
        return
    except Exception as exc:
        telemetry.outcome = "exception"
        logger.warning("Responses API stream failed (%s)", type(exc).__name__)
        yield _provider_error_event()
        return

    finally:
        telemetry.finish()

    if incomplete_reason is not None:
        logger.warning("Responses API output incomplete: %s", incomplete_reason)
        yield "data: " + json.dumps({
            "type": "error",
            "code": "OUTPUT_LIMIT_REACHED",
            "retryable": False,
            "reason": incomplete_reason,
            "message": "توقفت الإجابة قبل اكتمالها بسبب حد الطول.",
        }) + "\n\n"
        return
    if not completed:
        yield "data: " + json.dumps({
            "type": "error",
            "code": "LLM_STREAM_INTERRUPTED",
            "retryable": True,
            "message": "انقطع بث مزود الذكاء الاصطناعي قبل اكتماله.",
        }) + "\n\n"
        return

    # ── Resolve filenames (post-stream, non-blocking) ───────────────────
    file_citations = [
        item for item in raw_citations if item.get("type") != "url_citation"
    ]
    url_citations = [
        item for item in raw_citations if item.get("type") == "url_citation"
    ]
    citations = await _resolve_citations(file_citations)

    citation_list: list[dict] = [
        {
            "type": "file_citation",
            "file_id": c.file_id,
            "filename": c.filename,
            "index": c.index,
        }
        for c in citations
    ]
    seen_urls: set[str] = set()
    for citation in url_citations:
        if citation["url"] in seen_urls:
            continue
        seen_urls.add(citation["url"])
        citation_list.append(citation)

    if url_citations:
        links = []
        answer_text = "".join(full_text_parts)
        for citation in citation_list:
            if citation.get("type") == "url_citation" and f"]({citation['url']})" not in answer_text:
                title = citation.get("title") or citation["url"]
                links.append(f"- [{title}]({citation['url']})")
        if links:
            sources = "\n\n**المصادر / Sources:**\n" + "\n".join(links)
            yield "data: " + json.dumps(
                {"type": "chunk", "content": sources}
            ) + "\n\n"

    done = {"type": "done", "citations": citation_list}
    # Internal metadata consumed by chat_service for claim validation.  The
    # service removes it before forwarding the final SSE event to clients.
    if evidence_texts:
        done["evidence"] = evidence_texts
    if usage is not None:
        done["usage"] = usage
    yield "data: " + json.dumps(done) + "\n\n"


async def _resolve_citations(
    raw: list[dict],
) -> list[Citation]:
    """Deduplicate file IDs and fetch filenames from the Files API."""
    if not raw:
        return []

    client = get_openai_client()
    seen_ids: dict[str, dict] = {}
    for item in raw:
        if item["file_id"] not in seen_ids:
            seen_ids[item["file_id"]] = item

    async def fetch_name(file_id: str) -> tuple[str, str]:
        try:
            file_obj = await client.files.retrieve(file_id)
            return file_id, file_obj.filename
        except Exception:
            return file_id, file_id  # fallback to ID

    filename_map = {fid: item["filename"] for fid, item in seen_ids.items() if item.get("filename")}
    results = await asyncio.gather(*(fetch_name(fid) for fid in seen_ids if fid not in filename_map))
    filename_map.update(results)

    return [
        Citation(
            file_id=fid,
            filename=filename_map.get(fid, fid),
            index=seen_ids[fid]["index"],
        )
        for fid in seen_ids
    ]
