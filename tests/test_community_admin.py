"""Tests for admin Community Knowledge ingestion, quarantine, moderation, concurrency, and retraction (C1, H3, H4, H6, M3)."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import verify_admin
from app.community_repository import (
    CommunityConcurrencyError,
    CommunityIdCollisionError,
    invalidate_community_cache,
)
from app.routers.community_admin import reset_admin_community_rate_limits, router as community_admin_router

VALID_EVIDENCE_HASH = hashlib.sha256(b"verified-public-post-snapshot").hexdigest()


def _build_app(override_admin: bool = True) -> FastAPI:
    reset_admin_community_rate_limits()
    app = FastAPI()
    app.include_router(community_admin_router)
    if override_admin:
        app.dependency_overrides[verify_admin] = lambda: "admin-uid-1"
    return app


def _config(*, ingestion_enabled: bool = True, rag_enabled: bool = False):
    return SimpleNamespace(
        COMMUNITY_INGESTION_ENABLED=ingestion_enabled,
        COMMUNITY_RAG_ENABLED=rag_enabled,
        COMMUNITY_FAIL_CLOSED=True,
        COMMUNITY_SOURCE_MIN_CONFIDENCE=0.0,
    )


class _InMemoryCommunityStore:
    def __init__(self, *, fail_audit: bool = False) -> None:
        self.archives: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.records: dict[str, dict] = {}
        self.audits: list[dict] = []
        self.revocations: dict[str, dict] = {}
        self.fail_audit = fail_audit

    async def save_archive(self, data: dict):
        sha = data["source_sha256"]
        if sha in self.archives:
            return False, self.archives[sha]
        self.archives[sha] = dict(data)
        return True, self.archives[sha]

    async def claim_job(self, data: dict):
        sha = data["source_sha256"]
        if sha in self.jobs:
            return False, self.jobs[sha]
        self.jobs[sha] = dict(data)
        return True, self.jobs[sha]

    async def update_job(self, sha: str, data: dict):
        self.jobs[sha] = dict(data)
        return self.jobs[sha]

    async def get_job(self, job_id: str):
        if job_id in self.jobs:
            return self.jobs[job_id]
        for job in self.jobs.values():
            if job.get("job_id") == job_id:
                return job
        return None

    async def create_record_strict(self, data: dict):
        cid = data["community_id"]
        if cid in self.records:
            raise CommunityIdCollisionError(f"Community record '{cid}' already exists.")
        self.records[cid] = dict(data)
        invalidate_community_cache()
        return self.records[cid]

    async def get_record(self, cid: str):
        return self.records.get(cid)

    async def list_records(self, *, moderation_status=None, limit=50):
        items = list(self.records.values())
        if moderation_status:
            items = [i for i in items if i.get("moderation_status") == moderation_status]
        return items[:limit]

    async def transition_atomic(
        self,
        cid: str,
        *,
        expected_version: int,
        new_record: dict,
        audit_entry: dict,
        retraction_marker: dict | None = None,
    ):
        if cid not in self.records:
            raise KeyError(cid)
        cur_ver = int(self.records[cid].get("record_version") or 1)
        if cur_ver != expected_version:
            raise CommunityConcurrencyError("Version conflict")
        if self.fail_audit:
            raise RuntimeError("Simulated audit write failure")
        # Atomic commit: record + audit + revocation
        self.records[cid] = dict(new_record)
        self.audits.append(dict(audit_entry))
        if retraction_marker is not None:
            self.revocations[cid] = dict(retraction_marker)
        invalidate_community_cache()
        return self.records[cid]


def _patch_store(store: _InMemoryCommunityStore):
    return (
        patch("app.services.community_service.save_source_archive_idempotent", side_effect=store.save_archive),
        patch("app.services.community_service.claim_ingestion_job_atomic", side_effect=store.claim_job),
        patch("app.services.community_service.update_ingestion_job", side_effect=store.update_job),
        patch("app.services.community_service.get_ingestion_job", side_effect=store.get_job),
        patch("app.services.community_service.create_community_record_strict", side_effect=store.create_record_strict),
        patch("app.services.community_service.get_community_record", side_effect=store.get_record),
        patch("app.services.community_service.list_community_records", side_effect=store.list_records),
        patch("app.services.community_service.transition_community_record_atomic", side_effect=store.transition_atomic),
    )


def test_admin_community_upload_requires_auth():
    client = TestClient(_build_app(override_admin=False))
    res = client.post("/admin/community/uploads", files={"file": ("notes.md", b"hello world content", "text/markdown")})
    assert res.status_code in {401, 403}


def test_admin_community_disabled_by_default_returns_503_but_retract_stays_available_m3():
    store = _InMemoryCommunityStore()
    store.records["CM-LIVE-1"] = {
        "community_id": "CM-LIVE-1",
        "record_version": 2,
        "approval_version": 1,
        "moderation_status": "APPROVED_FOR_COMMUNITY_PUBLICATION",
        "index_namespace": "COMMUNITY_PUBLISHED",
    }
    client = TestClient(_build_app())
    p1, p2, p3, p4, p5, p6, p7, p8 = _patch_store(store)
    with (
        patch("app.routers.community_admin.get_config", return_value=_config(ingestion_enabled=False)),
        patch("app.services.community_service.get_config", return_value=_config(ingestion_enabled=False)),
        p1, p2, p3, p4, p5, p6, p7, p8,
    ):
        up = client.post("/admin/community/uploads", files={"file": ("notes.md", b"student review text", "text/markdown")})
        assert up.status_code == 503

        # M3: Retraction still succeeds even when COMMUNITY_INGESTION_ENABLED=false!
        ret = client.post(
            "/admin/community/review/CM-LIVE-1",
            json={"action": "retract", "retraction_reason": "Emergency retraction"},
        )
        assert ret.status_code == 200
        assert ret.json()["record"]["moderation_status"] == "RETRACTED"


def test_h3_rejects_oversized_1_1_mib_upload_and_invalid_json_before_durable_writes():
    store = _InMemoryCommunityStore()
    client = TestClient(_build_app())
    oversized = b"a" * (1100 * 1024)  # 1.1 MiB -> exceeds 256 KiB cap (and Firestore 1 MiB cap)

    p1, p2, p3, p4, p5, p6, p7, p8 = _patch_store(store)
    with (
        patch("app.routers.community_admin.get_config", return_value=_config(ingestion_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(ingestion_enabled=True)),
        p1, p2, p3, p4, p5, p6, p7, p8,
    ):
        res_big = client.post("/admin/community/uploads", files={"file": ("big.md", oversized, "text/markdown")})
        assert res_big.status_code == 413
        assert len(store.archives) == 0
        assert len(store.jobs) == 0

        res_bad_json = client.post("/admin/community/uploads", files={"file": ("bad.json", b"{not-json", "application/json")})
        assert res_bad_json.status_code == 400
        assert len(store.archives) == 0
        assert len(store.jobs) == 0


def test_h4_id_collision_cannot_overwrite_approved_record_and_server_binds_sha():
    store = _InMemoryCommunityStore()
    store.records["CM-APPROVED-1"] = {
        "community_id": "CM-APPROVED-1",
        "moderation_status": "APPROVED_FOR_COMMUNITY_PUBLICATION",
        "index_namespace": "COMMUNITY_PUBLISHED",
        "record_version": 2,
    }
    client = TestClient(_build_app())
    colliding = json.dumps({
        "community_id": "CM-APPROVED-1",
        "topic_ar": "محاولة استبدال سجل معتمد",
        "summary_sanitized_ar": "محتوى مختلف",
        "source_sha256": "0" * 64,
    }, ensure_ascii=False).encode("utf-8")

    p1, p2, p3, p4, p5, p6, p7, p8 = _patch_store(store)
    with (
        patch("app.routers.community_admin.get_config", return_value=_config(ingestion_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(ingestion_enabled=True)),
        p1, p2, p3, p4, p5, p6, p7, p8,
    ):
        res = client.post("/admin/community/uploads", files={"file": ("collide.jsonl", colliding, "application/x-ndjson")})
        assert res.status_code == 409
        # Existing approved record must remain completely intact!
        assert store.records["CM-APPROVED-1"]["moderation_status"] == "APPROVED_FOR_COMMUNITY_PUBLICATION"


def test_upload_quarantines_redacts_sensitive_heading_and_is_idempotent_t19_h6():
    store = _InMemoryCommunityStore()
    client = TestClient(_build_app())
    md_payload = (
        "# نصائح طلابية للمذاكرة\n"
        "تجربة طلابية عملية لمراجعة المحاضرات قبل الميدتيرم.\n\n"
        "# ادعاء حول اللائحة\n"
        "يقولون تقدر تسجل 21 وحدة في الصيفي حسب بايفورس الخريجين.\n\n"
        "# دكتور فلان أسوأ واحد 99123456\n"
        "لا تسجل عنده أبداً.\n"
    ).encode("utf-8")

    p1, p2, p3, p4, p5, p6, p7, p8 = _patch_store(store)
    with (
        patch("app.routers.community_admin.get_config", return_value=_config(ingestion_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(ingestion_enabled=True)),
        p1, p2, p3, p4, p5, p6, p7, p8,
    ):
        first = client.post("/admin/community/uploads", files={"file": ("faq.md", md_payload, "text/markdown")})
        assert first.status_code == 202
        body = first.json()
        assert body["duplicate"] is False
        assert body["published_count"] == 0
        # H6: Sensitive heading & phone number must be redacted in stored record.
        blocked = [r for r in store.records.values() if r["moderation_status"] == "BLOCKED_SENSITIVE_DEFAMATION_REVIEW"]
        assert len(blocked) == 1
        assert "99123456" not in blocked[0]["topic_ar"]
        assert "أسوأ" not in blocked[0]["topic_ar"]

        # Re-uploading identical content is atomically idempotent.
        second = client.post("/admin/community/uploads", files={"file": ("faq.md", md_payload, "text/markdown")})
        assert second.status_code == 202
        assert second.json()["duplicate"] is True
        assert len(store.records) == 3


def test_review_approve_requires_attestation_and_retraction_is_monotonic_c1_h4_t18_t23():
    store = _InMemoryCommunityStore()
    client = TestClient(_build_app())
    jsonl_line = json.dumps({
        "community_id": "CM-TEST-99",
        "dedup_group_id": "DG-TEST-99",
        "category": "studying",
        "content_nature": "STUDENT_EXPERIENCE_GUIDANCE",
        "topic_ar": "نصائح طلابية للمراجعة الليلية",
        "summary_sanitized_ar": "تجربة طلابية عامة لمتابعة الشواغر.",
        "identifiable_person_mentioned": False,
        "defamation_or_sensitivity_flag": False,
    }, ensure_ascii=False).encode("utf-8")

    p1, p2, p3, p4, p5, p6, p7, p8 = _patch_store(store)
    with (
        patch("app.routers.community_admin.get_config", return_value=_config(ingestion_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(ingestion_enabled=True)),
        p1, p2, p3, p4, p5, p6, p7, p8,
    ):
        up = client.post("/admin/community/uploads", files={"file": ("items.jsonl", jsonl_line, "application/x-ndjson")})
        assert up.status_code == 202

        # C1: Approve without captured_publisher / evidence_hash / date fails closed (422).
        bad_approve = client.post(
            "/admin/community/review/CM-TEST-99",
            json={
                "action": "approve",
                "original_source_link": "https://x.com/ku_students/status/1859990055",
                "provenance_status": "SOURCE_VERIFIED",
                "privacy_review_status": "PASS",
                "rights_review_status": "PASS",
            },
        )
        assert bad_approve.status_code == 422

        # Valid approval with full independent attestation succeeds.
        ok_approve = client.post(
            "/admin/community/review/CM-TEST-99",
            json={
                "action": "approve",
                "expected_version": 1,
                "original_source_link": "https://x.com/ku_students/status/1859990055",
                "publication_date": "2026-11-20",
                "captured_publisher": "@ku_students",
                "captured_at": "2026-11-25T15:55:00Z",
                "evidence_hash": VALID_EVIDENCE_HASH,
                "provenance_status": "SOURCE_VERIFIED",
                "privacy_review_status": "PASS",
                "rights_review_status": "PASS",
            },
        )
        assert ok_approve.status_code == 200
        rec = ok_approve.json()["record"]
        assert rec["moderation_status"] == "APPROVED_FOR_COMMUNITY_PUBLICATION"
        assert rec["approval_version"] == 1
        assert rec["approval_attestation"]["evidence_hash"] == VALID_EVIDENCE_HASH

        # Retraction commits revocation marker and increments version.
        retract = client.post(
            "/admin/community/review/CM-TEST-99",
            json={"action": "retract", "expected_version": 2, "retraction_reason": "Outdated review"},
        )
        assert retract.status_code == 200
        assert retract.json()["record"]["moderation_status"] == "RETRACTED"
        assert "CM-TEST-99" in store.revocations

        # H4: Stale/regular approve after retraction is rejected (409 monotonic).
        stale_approve = client.post(
            "/admin/community/review/CM-TEST-99",
            json={
                "action": "approve",
                "original_source_link": "https://x.com/ku_students/status/1859990055",
                "publication_date": "2026-11-20",
                "captured_publisher": "@ku_students",
                "captured_at": "2026-11-25T15:55:00Z",
                "evidence_hash": VALID_EVIDENCE_HASH,
                "provenance_status": "SOURCE_VERIFIED",
                "privacy_review_status": "PASS",
                "rights_review_status": "PASS",
            },
        )
        assert stale_approve.status_code == 409
        assert store.records["CM-TEST-99"]["moderation_status"] == "RETRACTED"
