"""SSE chat, replay revocation, and legacy history tests for Community Knowledge (H1, H2, H5, H6, M1, M2, COMM-004)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.community_repository import _published_cache, invalidate_community_cache, query_published_community_records
from app.services.chat_service import process_chat
from app.services.community_policy import (
    APPROVAL_DIGEST_VERSION,
    compute_evidence_snapshot_sha256,
    compute_record_content_hash,
)
from app.services.community_replay_guard import filter_sse_event_for_replay
from app.services.request_coalescer import RequestCoalescer

VALID_EVIDENCE_HASH = compute_evidence_snapshot_sha256(
    canonical_source_url="https://x.com/ku_reviews/status/1859990042",
    captured_publisher="@ku_reviews",
    captured_at="2026-11-25T15:55:00Z",
    publication_date="2026-11-18",
    captured_snapshot_text="تجربة طلابية تصف شرح المحاسبة بالواضح مع التزام بالحضور.",
)


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
    for line in event.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:].strip())
    raise AssertionError(f"No data line in SSE event: {event!r}")


def _discard_task(coroutine):
    coroutine.close()


def _approved_community_record(**overrides) -> dict:
    base = {
        "community_id": "CM-PUB-01",
        "dedup_group_id": "DG-PUB-01",
        "independent_author_key_hash": "author-hash-42",
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
        "source_confidence": 0.9,
        "privacy_review_status": "PASS",
        "rights_review_status": "PASS",
        "moderation_reviewer_id": "mod-1",
        "moderation_reviewed_at": "2026-11-25T16:00:00Z",
        "valid_until": "2027-06-01",
        "source_sha256": "a" * 64,
        "source_archive_path": "restricted_archive/COMMUNITY_CANDIDATE/sample.jsonl",
        "record_version": 2,
        "approval_version": 1,
        "evidence_id": "ev-abcdef1234567890",
        "evidence_hash": VALID_EVIDENCE_HASH,
        "index_namespace": "COMMUNITY_PUBLISHED",
        "isolated_from_official_kb": True,
    }
    base.update(overrides)
    content_hash = compute_record_content_hash(base)
    base["approved_content_hash"] = content_hash
    base["approval_attestation"] = {
        "digest_version": APPROVAL_DIGEST_VERSION,
        "attestation_version": base["approval_version"],
        "approved_content_hash": content_hash,
        "evidence_id": base["evidence_id"],
        "evidence_hash": base["evidence_hash"],
        "evidence_captured_by": "verifier-2",
        "canonical_source_url": base["original_source_link"],
        "captured_publisher": "@ku_reviews",
        "captured_at": "2026-11-25T15:55:00Z",
        "publication_date": base["publication_date"],
        "reviewer_id": base["moderation_reviewer_id"],
        "reviewed_at": base["moderation_reviewed_at"],
        "privacy_decision": "PASS",
        "rights_decision": "PASS",
        "reputational_decision": "PASS",
    }
    return base


def test_m1_community_flag_off_preserves_legacy_tools_and_history():
    captured = {}

    async def stream(messages, system_prompt, tools=None, **_kwargs):
        captured["messages"] = messages
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
        patch(
            "app.services.chat_service.load_recent_messages",
            return_value=[{"isUser": False, "content": "LEGACY_UNANNOTATED_HISTORY_ALLOWED_WHEN_OFF"}],
        ),
        patch("app.services.chat_service.load_user_schedules", return_value=[]),
        patch("app.services.chat_service.save_user_message", return_value=True),
        patch("app.services.chat_service.save_assistant_message", return_value=True),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.retrieve_community_context") as retrieve_mock,
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    retrieve_mock.assert_not_called()
    assert captured["tools"] == []
    assert "LEGACY_UNANNOTATED_HISTORY_ALLOWED_WHEN_OFF" in json.dumps(captured["messages"], ensure_ascii=False)
    assert [e["type"] for e in events] == ["status", "chunk", "done"]


def test_comm004_finding1_coalescer_replay_blocks_revoked_pure_community_and_retains_official_on_mixed():
    """COMM-004 Finding 1: Retracting a Community source must prevent replaying revoked chunks
    on reconnect, while retaining the independently grounded Official section on mixed responses.
    """
    coalescer = RequestCoalescer()

    async def pure_community_factory():
        yield 'data: {"type":"status","status":"generating_answer"}\n\n'
        yield 'data: {"type":"chunk","content":"رأي طلابي قديم","source_kind":"community_opinion","community_source_versions":[{"community_id":"CM-PUB-01","approval_version":1}]}\n\n'
        yield 'data: {"type":"done","citations":[{"type":"url_citation","url":"https://x.com/ku_reviews/status/1859990042","source_kind":"community_opinion","community_id":"CM-PUB-01","approval_version":1}],"community_source_versions":[{"community_id":"CM-PUB-01","approval_version":1}]}\n\n'

    async def mixed_factory():
        yield 'data: {"type":"status","status":"searching_knowledge"}\n\n'
        yield 'data: {"type":"chunk","content":"ضوابط السحب الرسمية من اللائحة."}\n\n'
        yield 'data: {"type":"chunk","content":"رأي طلابي مسحوب","source_kind":"community_opinion","community_source_versions":[{"community_id":"CM-PUB-01","approval_version":1}]}\n\n'
        yield 'data: {"type":"done","citations":[{"type":"url_citation","url":"https://ku.edu.kw/regulations/add-drop"},{"type":"url_citation","url":"https://x.com/ku_reviews/status/1859990042","source_kind":"community_opinion","community_id":"CM-PUB-01","approval_version":1}],"community_source_versions":[{"community_id":"CM-PUB-01","approval_version":1}]}\n\n'

    async def run():
        # Initial generation succeeds while record is published.
        with patch("app.services.community_replay_guard.verify_records_still_published", return_value=[{"community_id": "CM-PUB-01", "approval_version": 1}]):
            _ = [e async for e in coalescer.stream("pure-key", pure_community_factory)]
            _ = [e async for e in coalescer.stream("mixed-key", mixed_factory)]

        # Source is now retracted -> verify_records_still_published returns [].
        coalescer.invalidate_community_replay("CM-PUB-01")
        with patch("app.services.community_replay_guard.verify_records_still_published", return_value=[]):
            pure_replay = [_payload(e) async for e in coalescer.stream("pure-key", pure_community_factory)]
            mixed_replay = [_payload(e) async for e in coalescer.stream("mixed-key", mixed_factory)]
        return pure_replay, mixed_replay

    pure_replay, mixed_replay = asyncio.run(run())

    # Pure Community replay emits safe tombstone error, never revoked text.
    assert len(pure_replay) == 1
    assert pure_replay[0]["type"] == "error"
    assert pure_replay[0]["code"] == "COMMUNITY_SOURCE_REVOKED"

    # Mixed replay retains Official chunk + Official citation, strips revoked Community chunk & citation.
    mixed_text = "".join(e.get("content", "") for e in mixed_replay if e["type"] == "chunk")
    assert "ضوابط السحب الرسمية" in mixed_text
    assert "رأي طلابي مسحوب" not in mixed_text
    done_ev = [e for e in mixed_replay if e["type"] == "done"][-1]
    assert done_ev["citations"] == [{"type": "url_citation", "url": "https://ku.edu.kw/regulations/add-drop"}]
    assert "community_source_versions" not in done_ev


def test_comm004_finding1_replay_guard_fails_closed_on_lookup_exception_and_unknown_provenance():
    """Lookup exception or missing version metadata on a Community chunk must fail closed."""
    revoked_chunk = 'id: 1-0\ndata: {"type":"chunk","content":"رأي","source_kind":"community_opinion","community_source_versions":[{"community_id":"CM-1","approval_version":1}]}\n\n'
    unknown_prov_chunk = 'id: 2-0\ndata: {"type":"chunk","content":"رأي مجهول","source_kind":"community_opinion"}\n\n'

    async def failing_check(_refs):
        raise RuntimeError("Redis/Firestore unreachable")

    async def run():
        out1, rev1, _ = await filter_sse_event_for_replay(revoked_chunk, revalidate_fn=failing_check)
        out2, rev2, _ = await filter_sse_event_for_replay(unknown_prov_chunk)
        return (out1, rev1), (out2, rev2)

    (out1, rev1), (out2, rev2) = asyncio.run(run())
    assert out1 is None and rev1 is True
    assert out2 is None and rev2 is True


def test_comm004_finding2_legacy_unannotated_history_excluded_when_community_rag_enabled():
    """When COMMUNITY_RAG_ENABLED=true, legacy unannotated assistant history is excluded fail-closed,
    while explicitly official-cited and non_community assistant history is preserved.
    """
    captured = {}

    async def stream(messages, system_prompt, tools=None, **_kwargs):
        captured["messages"] = messages
        yield 'data: {"type":"chunk","content":"شرح تعليمي"}\n\n'
        yield 'data: {"type":"done","citations":[]}\n\n'

    history_rows = [
        {"isUser": False, "content": "OFFICIAL_SAFE_HISTORY", "citations": [{"type": "file_citation", "file_id": "f-1"}]},
        {"isUser": False, "content": "LEGACY_UNANNOTATED_COMMUNITY_LEAK"},
        {"isUser": False, "content": "EXPLICIT_NON_COMMUNITY_HISTORY", "history_source_kind": "non_community"},
        {"isUser": True, "content": "سؤال سابق"},
    ]

    async def run():
        return [
            _payload(e)
            async for e in process_chat("uid", "sess", "اشرح لي مفهوم التكامل مع مثال")
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=_config(community_rag_enabled=True)),
        patch("app.services.chat_service.load_recent_messages", return_value=history_rows),
        patch("app.services.chat_service.load_user_schedules", return_value=[]),
        patch("app.services.chat_service.save_user_message", return_value=True),
        patch("app.services.chat_service.save_assistant_message", return_value=True),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        _ = asyncio.run(run())

    serialized = json.dumps(captured["messages"], ensure_ascii=False)
    assert "LEGACY_UNANNOTATED_COMMUNITY_LEAK" not in serialized
    assert "OFFICIAL_SAFE_HISTORY" in serialized
    assert "EXPLICIT_NON_COMMUNITY_HISTORY" in serialized


def test_h1_mixed_query_never_sends_community_text_to_official_generator_and_composes_separately():
    calls = []

    async def stream(messages, system_prompt, tools=None, **_kwargs):
        calls.append({"prompt": system_prompt, "tools": tools})
        yield 'data: {"type":"chunk","content":"ضوابط السحب الرسمية من اللائحة."}\n\n'
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

    assert "<community_opinion_data>" not in calls[0]["prompt"]
    assert "x.com/ku_reviews/status/1859990042" not in calls[0]["prompt"]
    assert calls[0]["tools"] == [{"type": "file_search", "vector_store_ids": ["vs-official"]}]
    full_text = "".join(e["content"] for e in events if e["type"] == "chunk")
    assert "ضوابط السحب الرسمية" in full_text
    assert "تجارب وآراء الطلبة (غير رسمية وموثّقة المصدر)" in full_text
    assert events[-1]["community_source_versions"] == [{"community_id": "CM-PUB-01", "approval_version": 1}]


def test_h2_warm_cache_checks_authoritative_revocation_before_serving():
    invalidate_community_cache()
    _published_cache["records"] = [_approved_community_record()]
    _published_cache["expires_at"] = 9999999999.0

    with patch("app.community_repository.verify_record_not_revoked", return_value=False):
        result = asyncio.run(query_published_community_records(limit=10, use_cache=True))

    invalidate_community_cache()
    assert result == []


def test_h5_malformed_record_never_aborts_sse_and_empty_provider_done_falls_back_safely():
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
    assert events[1]["source_kind"] == "community_opinion"
    assert events[1]["community_source_versions"] == [{"community_id": "CM-PUB-01", "approval_version": 1}]


def test_h6_t22_pure_community_excludes_private_history_schedules_and_study_context():
    """H6 / T22: Private history, schedules, and uploaded study context never reach pure Community generation."""
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
            return_value=[_approved_community_record()],
        ),
        patch("app.services.chat_service.stream_response", stream_should_not_run),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    assert [e["type"] for e in events] == ["status", "chunk", "done"]
    assert "لا تتوفر حالياً آراء" in events[1]["content"]
    assert events[2]["citations"] == []
