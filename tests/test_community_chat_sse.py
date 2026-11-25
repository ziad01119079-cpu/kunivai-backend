"""SSE chat orchestration tests for Community Knowledge isolation, retraction, and fail-closed behavior (H1, H2, H5, H6, M1, M2, M4)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.community_repository import _published_cache, invalidate_community_cache, query_published_community_records
from app.services.chat_service import process_chat
from app.services.community_policy import compute_record_content_hash

VALID_EVIDENCE_HASH = hashlib.sha256(b"verified-public-post-snapshot").hexdigest()


def _config(*, community_rag_enabled: bool, fail_closed: bool = True):
    return SimpleNamespace(
        HISTORY_TOKEN_BUDGET=3000,
        OPENAI_VECTOR_STORE_ID="vs-official",
        OPENAI_MODEL="test-model",
        COMMUNITY_RAG_ENABLED=community_rag_enabled,
        COMMUNITY_INGESTION_ENABLED=False,
        COMMUNITY_FAIL_CLOSED=fail_closed,
        COMMUNITY_SOURCE_MIN_CONFIDENCE=0.0,
    )


def _payload(event: str) -> dict:
    assert event.startswith("data: ")
    return json.loads(event[6:].strip())


def _discard_task(coroutine):
    coroutine.close()


def _approved_community_record(**overrides) -> dict:
    base = {
        "community_id": "CM-PUB-01",
        "dedup_group_id": "DG-PUB-01",
        "category": "professors",
        "content_nature": "PERSONAL_OPINION",
        "topic_ar": "رأي طلابي في دكتور مادة المحاسبة",
        "summary_sanitized_ar": "تجربة طلابية تصف شرح المحاسبة بالواضح مع التزام بالحضور.",
        "identifiable_person_mentioned": True,
        "reputational_review_status": "PASS",
        "defamation_or_sensitivity_flag": False,
        "moderation_status": "APPROVED_FOR_COMMUNITY_PUBLICATION",
        "original_source_link": "https://x.com/ku_reviews/status/1859990042",
        "publication_date": "2026-11-18",
        "provenance_status": "SOURCE_VERIFIED",
        "privacy_review_status": "PASS",
        "rights_review_status": "PASS",
        "moderation_reviewer_id": "mod-1",
        "moderation_reviewed_at": "2026-11-25T16:00:00Z",
        "record_version": 2,
        "approval_version": 1,
        "index_namespace": "COMMUNITY_PUBLISHED",
        "isolated_from_official_kb": True,
    }
    base.update(overrides)
    content_hash = compute_record_content_hash(base)
    base["approved_content_hash"] = content_hash
    base["approval_attestation"] = {
        "attestation_version": base["approval_version"],
        "approved_content_hash": content_hash,
        "canonical_source_url": base["original_source_link"],
        "captured_publisher": "@ku_reviews",
        "captured_at": "2026-11-25T15:55:00Z",
        "publication_date": base["publication_date"],
        "evidence_hash": VALID_EVIDENCE_HASH,
        "reviewer_id": base["moderation_reviewer_id"],
        "reviewed_at": base["moderation_reviewed_at"],
        "privacy_decision": "PASS",
        "rights_decision": "PASS",
        "reputational_decision": "PASS",
    }
    return base


def test_m1_community_flag_off_preserves_legacy_tools_and_never_reads_community():
    captured = {}

    async def stream(messages, system_prompt, tools=None, **_kwargs):
        captured["prompt"] = system_prompt
        captured["tools"] = tools
        yield 'data: {"type":"chunk","content":"رد عام"}\n\n'
        yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        return [
            _payload(e)
            async for e in process_chat("uid", "sess", "هل مطعم X مفتوح الحين؟")
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=_config(community_rag_enabled=False)),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", return_value=[]),
        patch("app.services.chat_service.save_user_message", return_value=True),
        patch("app.services.chat_service.save_assistant_message", return_value=True),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.retrieve_community_context") as retrieve_mock,
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    retrieve_mock.assert_not_called()
    assert captured["tools"] == []  # Did NOT trigger hosted web_search when flag is OFF!
    assert "<community_opinion_data>" not in captured["prompt"]
    assert [e["type"] for e in events] == ["status", "chunk", "done"]


def test_h1_mixed_query_never_sends_community_text_to_official_generator_and_composes_separately():
    """Codex H1 regression: official generator must NEVER see Community text, so an unrelated
    official citation can never attest a Community claim; sections are composed programmatically.
    """
    calls = []

    async def stream(messages, system_prompt, tools=None, **_kwargs):
        calls.append({"prompt": system_prompt, "tools": tools})
        if tools and tools[0]["type"] == "file_search":
            yield 'data: {"type":"chunk","content":"ضوابط السحب الرسمية من اللائحة."}\n\n'
            yield 'data: {"type":"done","citations":[{"type":"url_citation","url":"https://ku.edu.kw/regulations/add-drop","title":"لائحة السحب والإضافة"}]}\n\n'
        else:
            yield 'data: {"type":"chunk","content":"ضوابط السحب الرسمية من موقع الجامعة."}\n\n'
            yield 'data: {"type":"done","citations":[{"type":"url_citation","url":"https://ku.edu.kw/regulations/add-drop","title":"لائحة السحب والإضافة"}]}\n\n'

    async def run():
        return [
            _payload(e)
            async for e in process_chat(
                "uid",
                "sess",
                "ما شروط السحب والإضافة وشنو رأي الطلاب في دكتور المحاسبة؟",
            )
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=_config(community_rag_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(community_rag_enabled=True)),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", return_value=[]),
        patch("app.services.chat_service.save_user_message", return_value=True),
        patch("app.services.chat_service.save_assistant_message", return_value=True),
        patch(
            "app.services.community_service.query_published_community_records",
            return_value=[_approved_community_record()],
        ),
        patch("app.services.community_service.verify_record_not_revoked", return_value=True),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    # Official LLM prompt must NOT contain any Community text!
    assert "<community_opinion_data>" not in calls[0]["prompt"]
    assert "x.com/ku_reviews/status/1859990042" not in calls[0]["prompt"]
    assert calls[0]["tools"] == [{"type": "file_search", "vector_store_ids": ["vs-official"]}]

    chunks = [e["content"] for e in events if e["type"] == "chunk"]
    full_text = "".join(chunks)
    assert "ضوابط السحب الرسمية" in full_text
    assert "تجارب وآراء الطلبة (غير رسمية وموثّقة المصدر)" in full_text
    assert "https://x.com/ku_reviews/status/1859990042" in full_text
    assert events[-1]["type"] == "done"
    assert events[-1]["community_source_versions"] == [{"community_id": "CM-PUB-01", "approval_version": 1}]


def test_h2_warm_cache_checks_authoritative_revocation_before_serving():
    """Codex H2 regression: warm replica cache must drop a record retracted on another host."""
    invalidate_community_cache()
    _published_cache["records"] = [_approved_community_record()]
    _published_cache["expires_at"] = 9999999999.0

    with patch("app.community_repository.verify_record_not_revoked", return_value=False):
        result = asyncio.run(query_published_community_records(limit=10, use_cache=True))

    invalidate_community_cache()
    assert result == []


def test_h5_malformed_record_never_aborts_sse_and_empty_provider_done_falls_back_safely():
    """Codex H5 regression: malformed IPv6/list-type record is skipped safely inside SSE boundary,
    and an empty-completed provider stream never emits empty done with orphan citations.
    """
    malformed = {
        "community_id": "CM-MALFORMED",
        "content_nature": ["BAD_LIST"],
        "original_source_link": "https://[::1/bad",
    }

    async def empty_provider_stream(messages, system_prompt, tools=None, **_kwargs):
        yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        return [
            _payload(e)
            async for e in process_chat("uid", "sess", "شنو رأي الطلاب في دكتور مادة المحاسبة؟")
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=_config(community_rag_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(community_rag_enabled=True)),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", return_value=[]),
        patch("app.services.chat_service.save_user_message", return_value=True),
        patch("app.services.chat_service.save_assistant_message", return_value=True),
        patch(
            "app.services.community_service.query_published_community_records",
            return_value=[malformed, _approved_community_record()],
        ),
        patch("app.services.community_service.verify_record_not_revoked", return_value=True),
        patch("app.services.chat_service.stream_response", empty_provider_stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    assert [e["type"] for e in events] == ["status", "chunk", "done"]
    assert "https://x.com/ku_reviews/status/1859990042" in events[1]["content"]
    assert len(events[2]["citations"]) == 1


def test_h6_pure_community_excludes_private_history_schedules_and_study_context():
    """Codex H6 regression: private history, schedules, and uploaded study context must NEVER
    be passed to the pure Community generator.
    """
    captured = {}

    async def stream(messages, system_prompt, tools=None, **_kwargs):
        captured["messages"] = messages
        captured["prompt"] = system_prompt
        yield 'data: {"type":"chunk","content":"رأي طلابي مؤرخ."}\n\n'
        yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        return [
            _payload(e)
            async for e in process_chat(
                "uid",
                "sess",
                "شنو رأي الطلاب في دكتور مادة المحاسبة؟",
                system_context="PRIVATE_STUDY_NOTES_MARKER_999",
            )
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=_config(community_rag_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(community_rag_enabled=True)),
        patch(
            "app.services.chat_service.load_recent_messages",
            return_value=[{"isUser": True, "content": "PRIVATE_HISTORY_MARKER_888"}],
        ),
        patch(
            "app.services.chat_service.load_user_schedules",
            return_value=[{"student": {"name": "PRIVATE_STUDENT_777"}, "courses": []}],
        ),
        patch("app.services.chat_service.save_user_message", return_value=True),
        patch("app.services.chat_service.save_assistant_message", return_value=True),
        patch(
            "app.services.community_service.query_published_community_records",
            return_value=[_approved_community_record()],
        ),
        patch("app.services.community_service.verify_record_not_revoked", return_value=True),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    assert len(captured["messages"]) == 1
    assert "PRIVATE_HISTORY_MARKER_888" not in json.dumps(captured["messages"], ensure_ascii=False)
    assert "PRIVATE_STUDY_NOTES_MARKER_999" not in captured["prompt"]
    assert "PRIVATE_STUDENT_777" not in captured["prompt"]
    assert [e["type"] for e in events] == ["status", "chunk", "done"]


def test_m2_category_only_does_not_return_unrelated_course_opinion():
    """Codex M2 regression: an accounting professor opinion must NOT be returned for a physics professor query."""
    async def stream_should_not_run(*_args, **_kwargs):
        raise AssertionError("Should abstain when no topic-matching opinion exists")
        yield  # pragma: no cover

    async def run():
        return [
            _payload(e)
            async for e in process_chat("uid", "sess", "شنو رأي الطلاب في دكتور مادة الفيزياء؟")
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=_config(community_rag_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(community_rag_enabled=True)),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", return_value=[]),
        patch("app.services.chat_service.save_user_message", return_value=True),
        patch("app.services.chat_service.save_assistant_message", return_value=True),
        patch(
            "app.services.community_service.query_published_community_records",
            return_value=[_approved_community_record()],  # Accounting only!
        ),
        patch("app.services.chat_service.stream_response", stream_should_not_run),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    assert [e["type"] for e in events] == ["status", "chunk", "done"]
    assert "لا تتوفر حالياً آراء" in events[1]["content"]
    assert events[2]["citations"] == []
