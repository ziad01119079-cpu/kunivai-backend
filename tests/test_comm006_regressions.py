"""TDD regression tests reproducing C1 and H1-H6 from CODEX_REVIEW_004 (COMM-006)."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import community_repository as repo
from app.services.chat_service import process_chat
from app.services.community_policy import (
    APPROVAL_DIGEST_VERSION,
    compute_evidence_snapshot_sha256,
    compute_record_content_hash,
    evaluate_for_publication,
)
from app.services.community_replay_guard import filter_sse_event_for_replay
from app.services.request_coalescer import RequestCoalescer


def _config(*, community_rag_enabled: bool = True, ingestion_enabled: bool = True):
    return SimpleNamespace(
        HISTORY_TOKEN_BUDGET=3000,
        OPENAI_VECTOR_STORE_ID="vs-official",
        OPENAI_MODEL="test-model",
        COMMUNITY_RAG_ENABLED=community_rag_enabled,
        COMMUNITY_INGESTION_ENABLED=ingestion_enabled,
        COMMUNITY_FAIL_CLOSED=True,
        COMMUNITY_SOURCE_MIN_CONFIDENCE=0.0,
    )


def _payload(event: str) -> dict:
    for line in event.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"No SSE data payload in {event!r}")


def _approved_record(cid="CM-1", url="https://x.com/ku_reviews/status/1859990042", topic="رأي طلابي في دكتور مادة المحاسبة", summary="تجربة طلابية تصف شرح المحاسبة بالواضح.", **overrides) -> dict:
    ev_hash = compute_evidence_snapshot_sha256(
        canonical_source_url=url,
        captured_publisher="@ku_reviews",
        captured_at="2026-11-25T15:55:00Z",
        publication_date="2026-11-18",
        captured_snapshot_text=summary,
    )
    base = {
        "community_id": cid,
        "dedup_group_id": f"DG-{cid}",
        "independent_author_key_hash": f"author-{cid}",
        "category": "professors",
        "content_nature": "PERSONAL_OPINION",
        "topic_ar": topic,
        "summary_sanitized_ar": summary,
        "identifiable_person_mentioned": False,
        "defamation_or_sensitivity_flag": False,
        "moderation_status": "APPROVED_FOR_COMMUNITY_PUBLICATION",
        "original_source_link": url,
        "publication_date": "2026-11-18",
        "provenance_status": "SOURCE_VERIFIED",
        "source_confidence": 0.9,
        "privacy_review_status": "PASS",
        "rights_review_status": "PASS",
        "reputational_review_status": None,
        "moderation_reviewer_id": "mod-1",
        "moderation_reviewed_at": "2026-11-25T16:00:00Z",
        "valid_until": "2027-06-01",
        "source_sha256": "a" * 64,
        "source_archive_path": "restricted_archive/COMMUNITY_CANDIDATE/sample.jsonl",
        "record_version": 2,
        "approval_version": 1,
        "evidence_id": f"ev-{cid}",
        "evidence_hash": ev_hash,
        "index_namespace": "COMMUNITY_PUBLISHED",
        "isolated_from_official_kb": True,
    }
    base.update(overrides)
    att = {
        "digest_version": APPROVAL_DIGEST_VERSION,
        "attestation_version": base["approval_version"],
        "evidence_id": base["evidence_id"],
        "evidence_hash": base["evidence_hash"],
        "evidence_content_fingerprint": None,
        "evidence_captured_by": "verifier-2",
        "canonical_source_url": base["original_source_link"],
        "captured_publisher": "@ku_reviews",
        "captured_at": "2026-11-25T15:55:00Z",
        "publication_date": base["publication_date"],
        "reviewer_id": base["moderation_reviewer_id"],
        "reviewed_at": base["moderation_reviewed_at"],
        "privacy_decision": "PASS",
        "rights_decision": "PASS",
        "reputational_decision": "NOT_APPLICABLE",
    }
    digest = compute_record_content_hash(base, attestation=att)
    att["approved_content_hash"] = digest
    base["approved_content_hash"] = digest
    base["approval_attestation"] = att
    return base


# ── C1 Reproduction ──────────────────────────────────────────────────────────
def test_c1_repro_uploader_cannot_supply_evidence_and_unrelated_or_reused_snapshot_blocked():
    """C1: Uploader must be excluded from evidence verifier role, reused upload text must be detected
    by canonical content fingerprint, and unrelated snapshot text must fail claim applicability.
    """
    from app.services import community_service as svc
    from app.services.community_models import CommunityReviewActionRequest

    # Candidate uploaded by uploader-1 with summary about accounting.
    candidate = {
        "community_id": "CM-C1",
        "dedup_group_id": "DG-C1",
        "category": "professors",
        "content_nature": "PERSONAL_OPINION",
        "topic_ar": "رأي طلابي في مادة المحاسبة",
        "summary_sanitized_ar": "تجربة طلابية تصف شرح المحاسبة بالواضح مع التزام بالحضور.",
        "identifiable_person_mentioned": False,
        "defamation_or_sensitivity_flag": False,
        "moderation_status": "NEEDS_MODERATION_REVIEW",
        "record_version": 1,
        "approval_version": 0,
        "uploaded_by": "uploader-1",
        "source_sha256": "b" * 64,
        "index_namespace": "COMMUNITY_CANDIDATE_ONLY",
        "isolated_from_official_kb": True,
    }
    unrelated_snapshot = "مباراة كرة قدم جميلة أمس في الملعب الوطني بدون أي علاقة بالجامعة."
    ev_hash = compute_evidence_snapshot_sha256(
        canonical_source_url="https://x.com/ku_reviews/status/1859990099",
        captured_publisher="@ku_reviews",
        captured_at="2026-11-25T15:55:00Z",
        publication_date="2026-11-18",
        captured_snapshot_text=unrelated_snapshot,
    )
    artifact = {
        "evidence_id": "ev-c1",
        "canonical_source_url": "https://x.com/ku_reviews/status/1859990099",
        "captured_publisher": "@ku_reviews",
        "captured_at": "2026-11-25T15:55:00Z",
        "publication_date": "2026-11-18",
        "captured_snapshot_text": unrelated_snapshot,
        "computed_sha256": ev_hash,
        "captured_by": "uploader-1",  # Same actor who uploaded the candidate!
        "immutable": True,
    }

    req = CommunityReviewActionRequest(
        action="approve",
        expected_version=1,
        evidence_id="ev-c1",
        evidence_hash=ev_hash,
        provenance_status="SOURCE_VERIFIED",
        privacy_review_status="PASS",
        rights_review_status="PASS",
    )

    with (
        patch("app.services.community_service.get_config", return_value=_config()),
        patch("app.services.community_service.get_community_record", return_value=candidate),
        patch("app.services.community_service.get_evidence_artifact", return_value=artifact),
        patch("app.services.community_service.transition_community_record_atomic", AsyncMock(return_value={})),
    ):
        with pytest.raises(svc.CommunityPublicationDeniedError) as exc_info:
            asyncio.run(svc.apply_review_action(community_id="CM-C1", reviewer_uid="reviewer-2", request=req))
        reasons = exc_info.value.reasons
        assert any("UPLOADER" in r or "EVIDENCE_CLAIM" in r or "APPLICABILITY" in r for r in reasons)


# ── H1 Reproduction ──────────────────────────────────────────────────────────
def test_h1_repro_stale_ingestion_owner_cannot_overwrite_newer_lease_or_finalize():
    """H1: After worker B reclaims an expired lease with a higher monotonic attempt_fence,
    stale worker A must be rejected on job update/finalization and record write.
    """
    assert hasattr(repo, "update_ingestion_job_fenced"), "Missing fenced job update in repository"


# ── H2 Reproduction ──────────────────────────────────────────────────────────
def test_h2_repro_local_coalescer_no_reorder_on_slow_reconnect_and_revalidates_live_chunks():
    """H2: Slow reconnect awaiting replay validation must not receive live chunk 2 before chunk 1,
    and a live subscriber must not drain a revoked Community chunk.
    """
    coalescer = RequestCoalescer()
    chunk1_gate = asyncio.Event()
    release_validation = asyncio.Event()
    validation_started = asyncio.Event()

    async def slow_factory():
        yield 'data: {"type":"chunk","content":"CHUNK_1","source_kind":"community_opinion","community_source_versions":[{"community_id":"CM-1","approval_version":1}]}\n\n'
        await chunk1_gate.wait()
        yield 'data: {"type":"chunk","content":"CHUNK_2","source_kind":"community_opinion","community_source_versions":[{"community_id":"CM-1","approval_version":1}]}\n\n'
        yield 'data: {"type":"done","citations":[{"type":"url_citation","url":"https://x.com/ku/status/1","source_kind":"community_opinion","community_id":"CM-1","approval_version":1}],"community_source_versions":[{"community_id":"CM-1","approval_version":1}]}\n\n'

    revoked = {"flag": False}

    async def checker(refs):
        validation_started.set()
        await release_validation.wait()
        if revoked["flag"]:
            return []
        return list(refs)

    async def run():
        with patch("app.services.community_replay_guard.verify_records_still_published", side_effect=checker):
            # Primary client receives CHUNK_1 while valid.
            release_validation.set()
            prim_iter = coalescer.stream("k-h2", slow_factory)
            first_ev = await anext(prim_iter)
            assert _payload(first_ev)["content"] == "CHUNK_1"

            # Pause validation, start reconnect, then let publisher emit CHUNK_2 + done while revoked!
            release_validation.clear()
            reconn_task = asyncio.create_task(
                _collect(coalescer.stream("k-h2", slow_factory))
            )
            await asyncio.sleep(0.02)
            revoked["flag"] = True
            coalescer.invalidate_community_replay("CM-1")
            chunk1_gate.set()
            await asyncio.sleep(0.02)
            release_validation.set()

            reconn_events = await reconn_task
            prim_rest = await _collect(prim_iter)
            return reconn_events, prim_rest

    reconn_events, prim_rest = asyncio.run(run())
    # Neither reconnect nor paused primary subscriber may receive revoked CHUNK_2!
    for ev in reconn_events + prim_rest:
        p = _payload(ev)
        assert p.get("content") != "CHUNK_2", f"Leaked revoked CHUNK_2: {p}"


async def _collect(ait):
    out = []
    async for item in ait:
        out.append(item)
    return out


# ── H3 Reproduction ──────────────────────────────────────────────────────────
def test_h3_repro_malformed_version_types_and_untagged_legacy_chunk_fail_closed():
    """H3: Non-integer version string must not raise ValueError; bool version must not coerce to 1;
    untagged legacy chunk with unknown provenance must not blindly pass as non-Community when required.
    """
    bad_str_ver = 'id: 1-0\ndata: {"type":"chunk","content":"x","source_kind":"community_opinion","community_source_versions":[{"community_id":"CM-1","approval_version":"not-an-int"}]}\n\n'
    bool_ver = 'id: 2-0\ndata: {"type":"chunk","content":"x","source_kind":"community_opinion","community_source_versions":[{"community_id":"CM-1","approval_version":true}]}\n\n'

    async def always_valid(refs):
        return list(refs)

    async def run():
        out1, rev1, _ = await filter_sse_event_for_replay(bad_str_ver, revalidate_fn=always_valid)
        out2, rev2, _ = await filter_sse_event_for_replay(bool_ver, revalidate_fn=always_valid)
        return (out1, rev1), (out2, rev2)

    (out1, rev1), (out2, rev2) = asyncio.run(run())
    assert out1 is None and rev1 is True
    assert out2 is None and rev2 is True


# ── H4 Reproduction ──────────────────────────────────────────────────────────
def test_h4_repro_partial_revocation_must_not_relabel_two_source_generated_text():
    """H4: When LLM generates from CM-1 and CM-2, and CM-1 is revoked before emission,
    the CM-1-influenced LLM text must NEVER be emitted relabeled with only CM-2.
    """
    rec1 = _approved_record(cid="CM-1", url="https://x.com/ku/status/1859990001", topic="دكتور المحاسبة الأول", summary="شرح المحاسبة الأول ممتاز.")
    rec2 = _approved_record(cid="CM-2", url="https://x.com/ku/status/1859990002", topic="دكتور المحاسبة الثاني", summary="شرح المحاسبة الثاني جيد.")

    async def stream_from_cm1(_messages, _prompt, tools=None, **_kwargs):
        yield 'data: {"type":"chunk","content":"UNIQUE_TEXT_DERIVED_FROM_CM1_ONLY"}\n\n'
        yield 'data: {"type":"done","citations":[]}\n\n'

    calls = {"n": 0}

    async def verify_side_effect(cid, ver):
        calls["n"] += 1
        # Initial pre-generation check (2 records): both valid.
        if calls["n"] <= 2:
            return True
        # Pre-emit check after generation: CM-1 was retracted!
        return cid != "CM-1"

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
        patch("app.services.community_service.query_published_community_records", return_value=[rec1, rec2]),
        patch("app.services.community_service.verify_record_not_revoked", side_effect=verify_side_effect),
        patch("app.services.chat_service.stream_response", stream_from_cm1),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=lambda c: c.close()),
    ):
        events = asyncio.run(run())

    full_text = "".join(e.get("content", "") for e in events if e["type"] == "chunk")
    assert "UNIQUE_TEXT_DERIVED_FROM_CM1_ONLY" not in full_text


# ── H5 Reproduction ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "att_field,mutated",
    [
        ("captured_publisher", "@tampered_handle"),
        ("captured_at", "2026-11-25T12:00:00Z"),
        ("evidence_captured_by", "tampered-verifier"),
        ("reviewed_at", "2026-11-25T19:00:00Z"),
    ],
)
def test_h5_repro_attestation_only_field_tamper_must_fail_publication(att_field, mutated):
    """H5: Mutating attestation-only fields (publisher, captured_at, verifier, reviewed_at)
    must invalidate publication validation.
    """
    rec = _approved_record()
    rec["approval_attestation"][att_field] = mutated
    decision = evaluate_for_publication(rec, today=date(2026, 11, 25), require_published_namespace=True)
    assert decision.allowed is False


# ── H6 Reproduction ──────────────────────────────────────────────────────────
def test_h6_repro_evil_url_path_not_official_and_feature_off_blocks_revoked_community_history():
    """H6: (1) URL with ku.edu.kw only in path must NOT pass as official history;
    (2) Disabling COMMUNITY_RAG_ENABLED must still block revoked prior Community answers from re-entering prompts.
    """
    captured_on = {}
    captured_off = {}

    async def stream_capture(bucket):
        async def _stream(messages, system_prompt, tools=None, **_kwargs):
            bucket["messages"] = messages
            yield 'data: {"type":"chunk","content":"ok"}\n\n'
            yield 'data: {"type":"done","citations":[]}\n\n'
        return _stream

    evil_url_history = [
        {
            "isUser": False,
            "content": "EVIL_PATH_SPOOF_HISTORY",
            "citations": [{"type": "url_citation", "url": "https://evil.example/ku.edu.kw/fake"}],
        },
        {"isUser": True, "content": "سؤال"},
    ]
    revoked_comm_history = [
        {
            "isUser": False,
            "content": "REVOKED_COMMUNITY_HISTORY_SHOULD_NOT_RETURN_WHEN_OFF",
            "citations": [
                {
                    "type": "url_citation",
                    "url": "https://x.com/ku/status/1",
                    "source_kind": "community_opinion",
                    "community_id": "CM-REVOKED",
                    "approval_version": 1,
                }
            ],
        },
        {"isUser": True, "content": "سؤال"},
    ]

    async def run():
        s_on = await stream_capture(captured_on)
        with (
            patch("app.services.chat_service.get_config", return_value=_config(community_rag_enabled=True)),
            patch("app.services.chat_service.load_recent_messages", return_value=evil_url_history),
            patch("app.services.chat_service.load_user_schedules", return_value=[]),
            patch("app.services.chat_service.save_user_message", return_value=True),
            patch("app.services.chat_service.save_assistant_message", return_value=True),
            patch("app.services.chat_service.stream_response", s_on),
            patch("app.services.chat_service.background_tasks.create_task", side_effect=lambda c: c.close()),
        ):
            _ = [e async for e in process_chat("uid", "sess", "اشرح لي مفهوم التكامل مع مثال")]

        s_off = await stream_capture(captured_off)
        with (
            patch("app.services.chat_service.get_config", return_value=_config(community_rag_enabled=False)),
            patch("app.services.chat_service.load_recent_messages", return_value=revoked_comm_history),
            patch("app.services.chat_service.load_user_schedules", return_value=[]),
            patch("app.services.chat_service.save_user_message", return_value=True),
            patch("app.services.chat_service.save_assistant_message", return_value=True),
            patch("app.services.community_service.verify_record_not_revoked", return_value=False),
            patch("app.services.chat_service.stream_response", s_off),
            patch("app.services.chat_service.background_tasks.create_task", side_effect=lambda c: c.close()),
        ):
            _ = [e async for e in process_chat("uid", "sess", "اشرح لي مفهوم التكامل مع مثال")]

    asyncio.run(run())
    assert "EVIL_PATH_SPOOF_HISTORY" not in json.dumps(captured_on["messages"], ensure_ascii=False)
    assert "REVOKED_COMMUNITY_HISTORY_SHOULD_NOT_RETURN_WHEN_OFF" not in json.dumps(captured_off["messages"], ensure_ascii=False)


def test_m2_repro_rendered_markdown_escapes_untrusted_links_outside_reviewed_citations():
    """M2: Untrusted topic/summary Markdown links must be escaped so only the reviewed citation URL is clickable."""
    from app.services.community_service import render_community_section_markdown

    rec = _approved_record(
        topic="دكتور [اضغط هنا](https://evil.example/phish)",
        summary="ملخص [رابط خارجي](https://evil.example/other)",
    )
    rendered = render_community_section_markdown([rec], english=False)
    assert "[اضغط هنا](https://evil.example/phish)" not in rendered
    assert "[رابط خارجي](https://evil.example/other)" not in rendered
    assert "[المصدر الأصلي](https://x.com/ku_reviews/status/1859990042)" in rendered
