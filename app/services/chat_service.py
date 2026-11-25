"""
app/services/chat_service.py

Service layer for chat orchestration.
"""

import asyncio
import json
import logging
import time
from typing import AsyncIterator

from ..prompts import get_community_only_prompt, get_system_prompt, get_web_search_prompt
from .query_router import (
    QueryIntent,
    contextual_query_intent,
    is_general_academic_explanation,
    is_explicit_policy_request,
    ku_web_search_tool,
    should_fallback_to_web,
)
from .missed_query_service import analyze_and_log_missed_query
from .academic_calendar import (
    LAW_GUIDANCE_URL,
    calendar_prompt_context,
    format_calendar_answer,
    is_english_message,
    lookup_official_calendar,
    resolve_calendar_query,
)

from ..chat_repository import (
    load_recent_messages,
    save_assistant_message,
    save_user_message,
)
from ..config import get_config
from ..history_builder import build_history
from ..responses_api import stream_response
from ..schedule_repository import load_user_schedules
from .schedule_context import build_schedule_context
from .grounding import applicable_citations, missing_evidence_answer, needs_authoritative_policy, sanitize_university_id_answer
from .provider_usage import UsageTrace, current_usage
from .verified_answers import arithmetic_match, known_answer, request_clauses
from .response_policy import has_previous_answer, response_policy_for, response_policy_prompt
from .model_policy import model_options
from .community_service import (
    build_community_prompt_context,
    community_abstention_answer,
    community_citations_for_records,
    community_source_versions_for_records,
    render_community_section_markdown,
    retrieve_community_context,
    verify_records_still_published,
)
from ..task_tracker import background_tasks
from ..logger import chat_id_var, generation_fence_var, uid_var

logger = logging.getLogger(__name__)

PERSISTENCE_TIMEOUT_SECONDS = 3.0


async def process_chat(
    uid: str,
    session_id: str,
    message: str,
    system_context: str | None = None,
    correlation_id: str | None = None,
    generation_claim: str | None = None,
) -> AsyncIterator[str]:
    """
    Handle the core business logic of generating a chat stream.
    Saves the user message, builds context, streams from OpenAI,
    and saves the final response.
    """
    config = get_config()

    # Set context variables for structured logging
    chat_id_var.set(session_id)
    uid_var.set(uid)

    if correlation_id:
        logger.info("Starting chat request with correlation_id: %s", correlation_id)

    # Calculate how many Firestore docs to fetch based on token budget
    # ~50 tokens per message on average → budget / 50 + small headroom
    max_docs = max(10, config.HISTORY_TOKEN_BUDGET // 50 + 5)

    # Define the bounded write now, but start it only after history is read.
    # Otherwise the write can win the race and the current turn is both loaded
    # from Firestore and manually appended below.
    async def safe_save_user_message() -> bool:
        db_start = time.perf_counter()
        try:
            await asyncio.wait_for(
                save_user_message(
                    uid,
                    session_id,
                    message,
                    correlation_id=correlation_id,
                ),
                timeout=PERSISTENCE_TIMEOUT_SECONDS,
            )
            logger.info("Saved user message for session %s in %.3fs", session_id, time.perf_counter() - db_start)
            return True
        except asyncio.TimeoutError:
            logger.error(
                "Timed out saving user message for session %s after %.1fs",
                session_id,
                PERSISTENCE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # Do not retry an ambiguous commit: a retry with a random document
            # ID could duplicate the user turn if Firestore committed remotely.
            logger.error("Failed to save user message for session %s: %s", session_id, exc)
        return False

    # 1. Load history and every stored schedule version concurrently.
    db_load_start = time.perf_counter()
    async def safe_load_schedules() -> list[dict] | None:
        try:
            return await load_user_schedules(uid)
        except Exception as exc:
            logger.exception("Failed to load schedules for user %s: %s", uid, exc)
            return None

    try:
        raw_messages, schedules = await asyncio.gather(
            load_recent_messages(uid, session_id, max_docs),
            safe_load_schedules(),
        )
    except Exception:
        # Preserve best-effort persistence when a context read fails.
        await safe_save_user_message()
        raise

    user_message_saved = False
    db_latency = time.perf_counter() - db_load_start
    logger.info(
        "Firestore context load latency for session %s: %.3fs (schedules=%s)",
        session_id,
        db_latency,
        "unavailable" if schedules is None else len(schedules),
    )

    # Persist asynchronously after the history snapshot is complete. This keeps
    # the current request out of raw_messages without text-based deduplication,
    # so legitimate repeated questions remain intact.
    # 2. Build token-limited context (returns chronological list).
    # H6: Omit prior assistant messages whose cited Community sources were later retracted.
    community_rag_enabled = bool(getattr(config, "COMMUNITY_RAG_ENABLED", False))
    safe_raw_messages = raw_messages
    if community_rag_enabled and raw_messages:
        filtered_history_rows = []
        for row in raw_messages:
            row_citations = row.get("citations") if isinstance(row, dict) else None
            if isinstance(row_citations, list):
                community_refs = [
                    {
                        "community_id": c.get("community_id"),
                        "approval_version": c.get("approval_version") or 0,
                    }
                    for c in row_citations
                    if isinstance(c, dict) and c.get("source_kind") == "community_opinion" and c.get("community_id")
                ]
                if community_refs:
                    still_valid = await verify_records_still_published(community_refs)
                    if len(still_valid) != len(community_refs):
                        continue
            filtered_history_rows.append(row)
        safe_raw_messages = filtered_history_rows
    history = build_history(safe_raw_messages, config.HISTORY_TOKEN_BUDGET)

    # Convert to OpenAI input format
    history_dicts = [{"role": m.role, "content": m.content} for m in history]
    # Append the current user message at the end
    history_dicts.append({"role": "user", "content": message})

    # 3. Build system prompt with a compact structured view of all schedules.
    schedule_context = build_schedule_context(schedules)
    schedule_colleges = {
        str((schedule.get("student") or {}).get("college") or "").strip()
        for schedule in (schedules or [])
        if str((schedule.get("student") or {}).get("college") or "").strip()
    }
    schedule_college = next(iter(schedule_colleges)) if len(schedule_colleges) == 1 else None
    clauses = request_clauses(message)
    is_mixed = len(clauses) > 1
    direct_answer = None if is_mixed else known_answer(message, raw_messages)
    calendar_query = None
    verified_parts = []
    remaining = []
    for clause in clauses if is_mixed else [message]:
        answered = known_answer(clause, []) if is_mixed else direct_answer
        if answered is not None:
            if is_mixed and (answered.sources or arithmetic_match(clause)):
                verified_parts.append(answered)
            elif is_mixed:
                remaining.append(clause)
            continue
        clause_intent = contextual_query_intent(
            clause,
            safe_raw_messages,
            community_enabled=community_rag_enabled,
        )
        candidate = resolve_calendar_query(clause, [] if is_mixed else safe_raw_messages, schedule_college=schedule_college) if (
            clause_intent is not QueryIntent.PERSONAL_SCHEDULE
        ) else None
        if candidate and calendar_query is None:
            calendar_query = candidate
        else:
            remaining.append(clause)
    remaining_question = " ".join(remaining) if is_mixed else message
    remaining_intents = (
        {
            contextual_query_intent(clause, safe_raw_messages, community_enabled=community_rag_enabled)
            for clause in remaining
        }
        if is_mixed
        else set()
    )
    intent = contextual_query_intent(
        remaining_question,
        safe_raw_messages,
        community_enabled=community_rag_enabled,
    )
    has_mixed_official_and_community = community_rag_enabled and (
        QueryIntent.MIXED_OFFICIAL_COMMUNITY in remaining_intents
        or (
            QueryIntent.COMMUNITY_OPINION in remaining_intents
            and bool(remaining_intents & {QueryIntent.OFFICIAL_UNIVERSITY, QueryIntent.TIME_SENSITIVE_UNIVERSITY})
        )
    )
    if has_mixed_official_and_community:
        intent = QueryIntent.MIXED_OFFICIAL_COMMUNITY
    else:
        for official_intent in (QueryIntent.TIME_SENSITIVE_UNIVERSITY, QueryIntent.OFFICIAL_UNIVERSITY):
            if official_intent in remaining_intents:
                intent = official_intent
                break
    needs_generation = is_mixed and bool(remaining)

    # A standalone lesson without attached context can omit schedules. Once
    # a conversation or attachment exists, topic keywords alone cannot prove
    # the records are irrelevant to a reference such as "based on that".
    if (
        intent is QueryIntent.GENERAL_EDUCATIONAL and not raw_messages and not system_context
        and not is_mixed and len(message.split()) >= 4
        and not any(key in message.casefold() for key in (
            "جدول", "معدل", "مسجل", "مقرري", "درجات", "مادتي", "بيانات", "الملف",
            "قسمي", "my ", "grade", "gpa", "schedule", "uploaded",
        ))
    ):
        schedule_context = None
    calendar_resolution = None
    previous_answer = has_previous_answer(history_dicts[:-1])
    length_context = response_policy_prompt(message, previous_answer=previous_answer)
    shape = response_policy_for(message, previous_answer=previous_answer)

    def stream_chat(messages, prompt, *, tools, fallback=False):
        return stream_response(
            messages, prompt, tools=tools,
            **model_options(config, shape, rag=bool(tools), fallback=fallback),
        )
    academic_calendar_context = (
        calendar_prompt_context(calendar_query, calendar_resolution)
        if calendar_query
        else None
    )
    if academic_calendar_context:
        academic_calendar_context = f"{academic_calendar_context}\n\n{length_context}"
    else:
        academic_calendar_context = length_context
    system_prompt = get_system_prompt(
        system_context,
        schedule_context,
        academic_calendar_context,
    )

    # 4. Current-information requests go straight to the web; static official
    # and uncertain university intent retain the File Search -> web fallback.
    # H1/H5/H6: Official prompts NEVER receive Community text; Community lookup
    # runs inside the SSE error boundary below.
    is_community_only = community_rag_enabled and intent is QueryIntent.COMMUNITY_OPINION
    is_mixed_community = community_rag_enabled and intent is QueryIntent.MIXED_OFFICIAL_COMMUNITY
    community_citations: list[dict] = []
    community_source_versions: list[dict] = []

    require_official = (
        intent not in {QueryIntent.PERSONAL_SCHEDULE, QueryIntent.COMMUNITY_OPINION}
        and not is_general_academic_explanation(remaining_question)
        and (
            is_mixed_community
            or needs_authoritative_policy(remaining_question)
            or is_explicit_policy_request(remaining_question)
        )
    )
    use_web_search = intent is QueryIntent.TIME_SENSITIVE_UNIVERSITY
    use_file_search = (
        not is_community_only
        and (
            (require_official and not use_web_search)
            or intent in {
                QueryIntent.OFFICIAL_UNIVERSITY,
                QueryIntent.MIXED_OFFICIAL_COMMUNITY,
                QueryIntent.UNCERTAIN,
            }
        )
    )
    tools = []
    if use_file_search:
        tools.append({
            "type": "file_search",
            "vector_store_ids": [config.OPENAI_VECTOR_STORE_ID],
        })

    # 5. Stream to client, collect result for Firestore
    accumulated_text: list[str] = []
    accumulated_citations: list[dict] = []
    ttft: float | None = None
    file_search_attempted = False
    provider_failed = False
    done_payload: dict | None = None
    assistant_persistence_attempted = False

    openai_start = time.perf_counter()

    def parse_sse(sse_line: str) -> dict | None:
        if not sse_line.startswith("data: "):
            return None
        try:
            return json.loads(sse_line[6:].strip())
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode SSE line: %s", exc)
            return None

    def stream_error(error: dict | None = None) -> str:
        # Usage and annotations from unfinished text are internal metadata.
        payload = (
            {key: value for key, value in error.items() if key not in {"usage", "citations", "evidence"}}
            if error else {
                "type": "error", "code": "LLM_STREAM_INTERRUPTED", "retryable": True,
                "message": "انقطع بث مزود الذكاء الاصطناعي قبل اكتماله.",
            }
        )
        return "data: " + json.dumps(payload) + "\n\n"

    def public_done(payload: dict) -> str:
        public_payload = {key: value for key, value in payload.items() if key != "evidence"}
        public_payload["citations"] = accumulated_citations
        return "data: " + json.dumps(public_payload) + "\n\n"

    async def persist_assistant_message() -> bool:
        nonlocal assistant_persistence_attempted
        if assistant_persistence_attempted:
            return False
        final_text = "".join(accumulated_text).strip()
        if not final_text:
            return False
        assistant_persistence_attempted = True
        if not user_message_saved:
            logger.error(
                "Skipping assistant persistence because the user turn was not saved for session %s",
                session_id,
            )
            return False
        try:
            save_kwargs = {
                "uid": uid,
                "session_id": session_id,
                "content": final_text,
                "citations": accumulated_citations,
            }
            if correlation_id:
                save_kwargs["correlation_id"] = correlation_id
            generation_fence = generation_fence_var.get()
            if generation_fence is not None:
                save_kwargs["generation_fence"] = generation_fence
            if generation_claim is not None:
                save_kwargs["generation_claim"] = generation_claim
            saved = await asyncio.wait_for(
                save_assistant_message(**save_kwargs),
                timeout=PERSISTENCE_TIMEOUT_SECONDS,
            )
            return saved is not False
        except asyncio.TimeoutError:
            logger.error(
                "Timed out saving assistant message for session %s after %.1fs",
                session_id,
                PERSISTENCE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.exception(
                "Failed to save assistant message for session %s: %s",
                session_id,
                exc,
            )
        return False

    # Start persistence after the history snapshot.  Yield the route status
    # first so a slow Firestore commit never delays the initial SSE response.
    user_save_task = asyncio.create_task(safe_save_user_message())
    background_tasks.tasks.add(user_save_task)
    user_save_task.add_done_callback(background_tasks.tasks.discard)

    trace = UsageTrace(route=(
        "mixed_verified" if needs_generation and (verified_parts or calendar_query) else
        "verified_direct" if direct_answer or verified_parts else "official_calendar" if calendar_query else intent.value
    ))
    trace_token = current_usage.set(trace)

    def verify_retrieval(answer: str, citations: list[dict], evidence: list[str]) -> tuple[str, list[dict]]:
        applicable = applicable_citations(citations, require_official=require_official)
        # Never publish text based on a citation that was discarded at this boundary.
        if not applicable or len(applicable) != len(citations) or not answer:
            return missing_evidence_answer(message), []
        safe_answer, _ = sanitize_university_id_answer(message, answer, evidence)
        return safe_answer, applicable

    local_texts = []
    local_prefix_length = 0

    def add_provider_citations(items: list[dict]) -> None:
        for item in items:
            if local_prefix_length:
                item = item.copy()
                for key in ("start_index", "end_index", "index"):
                    if type(item.get(key)) is int:
                        item[key] += local_prefix_length
            accumulated_citations.append(item)

    try:
        initial_status = (
            "generating_answer" if direct_answer else
            "searching_ku_web"
            if calendar_query is not None or use_web_search
            else "searching_knowledge"
            if use_file_search
            else "generating_answer"
        )
        yield "data: " + json.dumps(
            {"type": "status", "status": initial_status}
        ) + "\n\n"
        if calendar_query is not None:
            # The official lookup uses the context snapshot and can run during the user write.
            user_message_saved, calendar_resolution = await asyncio.gather(
                asyncio.shield(user_save_task),
                lookup_official_calendar(calendar_query),
            )
        else:
            user_message_saved = await asyncio.shield(user_save_task)

        local_citations = []
        for result in ([direct_answer] if direct_answer else verified_parts):
            local_texts.append(result.text)
            local_citations.extend(result.citations())
        if calendar_query is not None:
            calendar_answer = format_calendar_answer(
                calendar_query, calendar_resolution, english=is_english_message(message)
            )
            local_texts.append(calendar_answer)
            if f"]({LAW_GUIDANCE_URL})" in calendar_answer:
                local_citations.append({
                    "type": "url_citation",
                    "url": LAW_GUIDANCE_URL,
                    "title": "Academic Guidance" if is_english_message(message) else "الإرشاد والتوجيه بكلية الحقوق",
                    "start_index": 0,
                    "end_index": 0,
                })
            local_citations.extend({
                "type": "url_citation",
                "url": source.url,
                "title": source.title_en if is_english_message(message) else source.title_ar,
                "start_index": 0,
                "end_index": 0,
            } for source in calendar_resolution.citation_sources)

        if local_texts:
            local_text = "\n\n".join(local_texts)
            accumulated_citations.extend(local_citations)
            if needs_generation:
                # The deterministic part has already been verified and will
                # be rendered separately. The model sees the original turn.
                continuation = (
                    "The verified application answer below is already shown. Complete every remaining subtask "
                    "from the user's full request. Avoid repeating or changing that evidence. Its sources support "
                    "only that part, so retrieve separate evidence for new KU policy claims where required. "
                    "Use inputs already supplied in this conversation. Ask only for missing inputs.\n"
                    "<verified_application_answer>\n" + local_text + "\n</verified_application_answer>"
                )
                academic_calendar_context += "\n\n" + continuation
                system_prompt = get_system_prompt(
                    system_context,
                    schedule_context,
                    academic_calendar_context,
                )
                local_text += "\n\n"
                local_prefix_length = len(local_text)
            accumulated_text.append(local_text)
            yield "data: " + json.dumps({"type": "chunk", "content": local_text}) + "\n\n"

        if local_texts and not needs_generation:
            done_payload = {"type": "done", "citations": local_citations}
        elif is_community_only:
            # H2/H5/H6: Isolated pure-Community path inside SSE error boundary.
            # Never forwards private history, schedules, or uploaded study context.
            community_result = await retrieve_community_context(remaining_question)
            surviving = (
                await verify_records_still_published(community_result.records)
                if community_result.records
                else []
            )
            if not surviving:
                abstention = community_abstention_answer(
                    message,
                    english=is_english_message(message),
                    unavailable=(community_result.status == "unavailable"),
                )
                accumulated_text.append(abstention)
                yield "data: " + json.dumps({"type": "chunk", "content": abstention}) + "\n\n"
                done_payload = {"type": "done", "citations": []}
            else:
                comm_prompt = get_community_only_prompt(build_community_prompt_context(surviving))
                comm_chunks: list[str] = []
                comm_done: dict | None = None
                comm_error: dict | None = None
                # Pass ONLY the current user message (no private history/schedules/context).
                async for sse_line in stream_chat(
                    [{"role": "user", "content": message}],
                    comm_prompt,
                    tools=[],
                ):
                    if ttft is None and '"type": "chunk"' in sse_line:
                        ttft = time.perf_counter() - openai_start
                    payload = parse_sse(sse_line)
                    if payload is None:
                        continue
                    if payload.get("type") == "error":
                        comm_error = payload
                        break
                    if payload.get("type") == "chunk":
                        comm_chunks.append(payload.get("content", ""))
                    elif payload.get("type") == "done":
                        comm_done = payload
                if comm_error is not None or comm_done is None:
                    provider_failed = True
                    yield stream_error(comm_error)
                    return

                # H2: Authoritative revocation check immediately before emitting/persisting.
                surviving = await verify_records_still_published(surviving)
                if not surviving:
                    abstention = community_abstention_answer(
                        message,
                        english=is_english_message(message),
                    )
                    accumulated_text.append(abstention)
                    yield "data: " + json.dumps({"type": "chunk", "content": abstention}) + "\n\n"
                    done_payload = {"type": "done", "citations": []}
                else:
                    comm_text = "".join(comm_chunks).strip()
                    # H5: Validate nonempty content before attaching citations and ending in done.
                    if not comm_text:
                        comm_text = render_community_section_markdown(
                            surviving,
                            english=is_english_message(message),
                        )
                    accumulated_text.append(comm_text)
                    community_citations = community_citations_for_records(surviving)
                    community_source_versions = community_source_versions_for_records(surviving)
                    yield "data: " + json.dumps({"type": "chunk", "content": comm_text}) + "\n\n"
                    done_payload = comm_done
        elif use_web_search:
            web_text: list[str] = []
            web_citations: list[dict] = []
            web_evidence: list[str] = []
            web_error: dict | None = None
            async for sse_line in stream_chat(
                history_dicts,
                get_web_search_prompt(
                    system_context,
                    schedule_context,
                    academic_calendar_context,
                ),
                tools=[ku_web_search_tool()],
            ):
                if ttft is None and '"type": "chunk"' in sse_line:
                    ttft = time.perf_counter() - openai_start
                payload = parse_sse(sse_line)
                if payload is not None:
                    if payload.get("type") == "chunk":
                        web_text.append(payload.get("content", ""))
                    elif payload.get("type") == "done":
                        web_citations.extend(payload.get("citations", []))
                        web_evidence.extend(payload.get("evidence", []))
                        done_payload = payload
                    elif payload.get("type") == "error":
                        web_error = payload
            if web_error is not None or done_payload is None:
                provider_failed = True
                yield stream_error(web_error)
                return
            answer, web_citations = verify_retrieval("".join(web_text).strip(), web_citations, web_evidence)
            accumulated_text.append(answer)
            add_provider_citations(web_citations)
            yield "data: " + json.dumps(
                {"type": "chunk", "content": answer}
            ) + "\n\n"
        elif use_file_search:
            file_search_attempted = True
            buffered_lines: list[str] = []
            retrieved_text: list[str] = []
            retrieved_citations: list[dict] = []
            retrieved_evidence: list[str] = []
            retrieved_done: dict | None = None

            async for sse_line in stream_chat(
                history_dicts, system_prompt, tools=tools
            ):
                payload = parse_sse(sse_line)
                if payload is None or payload.get("type") != "done":
                    buffered_lines.append(sse_line)
                if payload is None:
                    continue
                if payload.get("type") == "error":
                    provider_failed = True
                    yield stream_error(payload)
                    return
                if payload.get("type") == "chunk":
                    retrieved_text.append(payload.get("content", ""))
                elif payload.get("type") == "done":
                    retrieved_citations.extend(payload.get("citations", []))
                    retrieved_evidence.extend(payload.get("evidence", []))
                    retrieved_done = payload

            if retrieved_done is None:
                provider_failed = True
                yield stream_error()
                return

            retrieved_answer = "".join(retrieved_text).strip()
            usable_retrieved_citations = applicable_citations(retrieved_citations, require_official=require_official)
            if (
                retrieved_answer and len(usable_retrieved_citations) == len(retrieved_citations)
                and not should_fallback_to_web(retrieved_answer, usable_retrieved_citations)
            ):
                yield "data: " + json.dumps(
                    {"type": "status", "status": "generating_answer"}
                ) + "\n\n"
                safe_answer, was_sanitized = sanitize_university_id_answer(
                    message,
                    retrieved_answer,
                    retrieved_evidence,
                )
                add_provider_citations(retrieved_citations)
                done_payload = retrieved_done
                if was_sanitized:
                    accumulated_text.append(safe_answer)
                    yield "data: " + json.dumps(
                        {"type": "chunk", "content": safe_answer}
                    ) + "\n\n"
                else:
                    accumulated_text.extend(retrieved_text)
                    for sse_line in buffered_lines:
                        if ttft is None and '"type": "chunk"' in sse_line:
                            ttft = time.perf_counter() - openai_start
                        yield sse_line
            else:
                trace.fallback_reason = (
                    "inapplicable_file_citations" if retrieved_citations and len(usable_retrieved_citations) != len(retrieved_citations)
                    else "missing_file_citations" if not retrieved_citations
                    else "insufficient_file_answer"
                )
                yield "data: " + json.dumps(
                    {"type": "status", "status": "searching_ku_web"}
                ) + "\n\n"

                web_text: list[str] = []
                web_evidence: list[str] = []
                web_citations: list[dict] = []
                web_error: dict | None = None
                async for sse_line in stream_chat(
                    history_dicts,
                    get_web_search_prompt(
                        system_context,
                        schedule_context,
                        academic_calendar_context,
                    ),
                    tools=[ku_web_search_tool()],
                    fallback=True,
                ):
                    if ttft is None and '"type": "chunk"' in sse_line:
                        ttft = time.perf_counter() - openai_start
                    payload = parse_sse(sse_line)
                    if payload is not None:
                        if payload.get("type") == "chunk":
                            web_text.append(payload.get("content", ""))
                        elif payload.get("type") == "done":
                            web_citations.extend(payload.get("citations", []))
                            web_evidence.extend(payload.get("evidence", []))
                            done_payload = payload
                        elif payload.get("type") == "error":
                            web_error = payload
                if web_error is not None or done_payload is None:
                    provider_failed = True
                    yield stream_error(web_error)
                    return
                safe_answer, web_citations = verify_retrieval("".join(web_text).strip(), web_citations, web_evidence)
                accumulated_text.append(safe_answer)
                add_provider_citations(web_citations)
                yield "data: " + json.dumps(
                    {"type": "chunk", "content": safe_answer}
                ) + "\n\n"
        else:
            async for sse_line in stream_chat(
                history_dicts, system_prompt, tools=[]
            ):
                if ttft is None and '"type": "chunk"' in sse_line:
                    ttft = time.perf_counter() - openai_start
                payload = parse_sse(sse_line)
                if payload is not None:
                    if payload.get("type") == "error":
                        provider_failed = True
                        yield stream_error(payload)
                        return
                    if payload.get("type") == "chunk":
                        accumulated_text.append(payload.get("content", ""))
                    elif payload.get("type") == "done":
                        add_provider_citations(payload.get("citations", []))
                if payload is not None and payload.get("type") == "done":
                    done_payload = payload
                else:
                    yield sse_line

        # H1/H2/H5: For mixed queries, independently retrieve, verify, and programmatically
        # append the dated Community section AFTER official generation completes.
        if is_mixed_community and done_payload is not None:
            community_result = await retrieve_community_context(remaining_question)
            surviving = (
                await verify_records_still_published(community_result.records)
                if community_result.records
                else []
            )
            comm_section = "\n\n" + render_community_section_markdown(
                surviving,
                english=is_english_message(message),
                unavailable=(community_result.status == "unavailable"),
            )
            accumulated_text.append(comm_section)
            yield "data: " + json.dumps({"type": "chunk", "content": comm_section}) + "\n\n"
            if surviving:
                community_citations = community_citations_for_records(surviving)
                community_source_versions = community_source_versions_for_records(surviving)

        if done_payload is None:
            provider_failed = True
            yield stream_error()
            return

        # `done` means both messages have been committed in order (or that a
        # persistence failure was logged).  This makes immediate follow-ups see
        # the complete preceding turn and keeps `done` the final SSE event.
        if community_citations:
            add_provider_citations(community_citations)
        if community_source_versions:
            done_payload = {**done_payload, "community_source_versions": community_source_versions}
        if local_citations or community_citations:
            seen = set()
            unique_citations = []
            for citation in accumulated_citations:
                key = citation.get("url") or citation.get("file_id")
                if key and key in seen:
                    continue
                seen.add(key)
                unique_citations.append(citation)
            accumulated_citations[:] = unique_citations
        persisted = await persist_assistant_message()
        if generation_claim is not None and not persisted:
            yield 'data: ' + json.dumps({
                "type": "error", "code": "AI_PERSISTENCE_UNCONFIRMED", "retryable": False,
                "message": "تعذر تأكيد حفظ الإجابة. تحقق من سجل المحادثة.",
            }) + '\n\n'
        else:
            yield public_done(done_payload)

    except asyncio.CancelledError:
        logger.warning("Client disconnected during stream for session %s", session_id)
        raise
    except Exception:
        logger.exception("Chat stream failed for session %s", session_id)
        yield "data: " + json.dumps({
            "type": "error",
            "code": "CHAT_UPSTREAM_ERROR",
            "retryable": True,
            "message": "تعذر إكمال الإجابة مؤقتاً. حاول مرة أخرى.",
        }) + "\n\n"
    finally:
        openai_latency = time.perf_counter() - openai_start

        trace.summary("foreground", ttft=ttft, latency=openai_latency)

        # Persist partial text on disconnect/error.  Successful streams already
        # persisted before their final `done` event.
        final_text = "".join(accumulated_text).strip()
        if generation_claim is None and final_text and not assistant_persistence_attempted and not provider_failed:
            await persist_assistant_message()

        # 8. Background Task: Missed Query Analytics
        was_file_search_attempted = file_search_attempted
        try:
            should_analyze = (not local_texts or needs_generation) and not provider_failed and (
                (was_file_search_attempted and not accumulated_citations) or "عذراً" in final_text
            )
            if should_analyze:
                background_tasks.create_task(
                    analyze_and_log_missed_query(
                        query=message,
                        chat_id=session_id,
                        was_file_search_attempted=was_file_search_attempted,
                        model_used=config.OPENAI_MODEL
                    )
                )
        finally:
            current_usage.reset(trace_token)
