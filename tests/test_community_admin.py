"""Tests for admin Community Knowledge ingestion, resumable jobs, immutable evidence, MIME checks, and retraction (COMM-004)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import verify_admin
from app.community_repository import (
    CommunityConcurrencyError,
    CommunityIdCollisionError,
    CommunityIngestionInProgressError,
    invalidate_community_cache,
)
from app.routers.community_admin import reset_admin_community_rate_limits, router as community_admin_router


def _build_app(override_admin: bool = True, admin_uid: str = "admin-uid-1") -> FastAPI:
    reset_admin_community_rate_limits()
    app = FastAPI()
    app.include_router(community_admin_router)
    if override_admin:
        app.dependency_overrides[verify_admin] = lambda: admin_uid
    return app


def _config(*, ingestion_enabled: bool = True, rag_enabled: bool = False):
    return SimpleNamespace(
        COMMUNITY_INGESTION_ENABLED=ingestion_enabled,
        COMMUNITY_RAG_ENABLED=rag_enabled,
        COMMUNITY_FAIL_CLOSED=True,
        COMMUNITY_SOURCE_MIN_CONFIDENCE=0.0,
    )


class _InMemoryCommunityStore:
    def __init__(self, *, fail_on_record_index: int | None = None) -> None:
        self.archives: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.records: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}
        self.audits: list[dict] = []
        self.revocations: dict[str, dict] = {}
        self.fail_on_record_index = fail_on_record_index
        self._create_calls = 0

    async def create_evidence(self, data: dict):
        eid = data["evidence_id"]
        if eid in self.evidence:
            raise CommunityIdCollisionError(f"Evidence '{eid}' exists")
        self.evidence[eid] = dict(data)
        return self.evidence[eid]

    async def get_evidence(self, eid: str):
        return self.evidence.get(eid)

    async def save_archive(self, data: dict):
        sha = data["source_sha256"]
        if sha in self.archives:
            return False, self.archives[sha]
        self.archives[sha] = dict(data)
        return True, self.archives[sha]

    async def claim_or_resume_job(self, data: dict, *, now_epoch=None):
        sha = data["source_sha256"]
        now = now_epoch if now_epoch is not None else time.time()
        existing = self.jobs.get(sha)
        if existing is None:
            self.jobs[sha] = dict(data)
            return "claimed", self.jobs[sha]
        expected = existing.get("expected_record_ids") or []
        written = existing.get("record_ids") or []
        if existing.get("status") == "QUARANTINED" and expected and set(written) == set(expected):
            return "duplicate_completed", existing
        if existing.get("status") == "CLAIMED" and float(existing.get("lease_expires_at") or 0.0) > now:
            raise CommunityIngestionInProgressError("Job currently in progress")
        resumed = {
            **existing,
            "status": "CLAIMED",
            "attempt_id": data.get("attempt_id"),
            "lease_expires_at": data.get("lease_expires_at"),
            "expected_record_ids": data.get("expected_record_ids") or expected,
            "error": None,
        }
        self.jobs[sha] = resumed
        return "resumed", resumed

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

    async def create_record_strict(self, data: dict, *, allow_same_job_resume: bool = False):
        self._create_calls += 1
        if self.fail_on_record_index is not None and self._create_calls == self.fail_on_record_index:
            self.fail_on_record_index = None  # fail once, succeed on retry
            raise RuntimeError("Simulated mid-job write failure")
        cid = data["community_id"]
        if cid in self.records:
            existing = self.records[cid]
            if (
                allow_same_job_resume
                and existing.get("source_sha256") == data.get("source_sha256")
                and existing.get("ingestion_job_id") == data.get("ingestion_job_id")
                and existing.get("moderation_status") != "APPROVED_FOR_COMMUNITY_PUBLICATION"
                and existing.get("index_namespace") == "COMMUNITY_CANDIDATE_ONLY"
            ):
                return existing
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
        self.records[cid] = dict(new_record)
        self.audits.append(dict(audit_entry))
        if retraction_marker is not None:
            self.revocations[cid] = dict(retraction_marker)
        invalidate_community_cache()
        return self.records[cid]


def _patch_store(store: _InMemoryCommunityStore):
    return (
        patch("app.services.community_service.create_evidence_artifact_immutable", side_effect=store.create_evidence),
        patch("app.services.community_service.get_evidence_artifact", side_effect=store.get_evidence),
        patch("app.services.community_service.save_source_archive_idempotent", side_effect=store.save_archive),
        patch("app.services.community_service.claim_or_resume_ingestion_job", side_effect=store.claim_or_resume_job),
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
    patches = _patch_store(store)
    with (
        patch("app.routers.community_admin.get_config", return_value=_config(ingestion_enabled=False)),
        patch("app.services.community_service.get_config", return_value=_config(ingestion_enabled=False)),
        patches[0], patches[1], patches[2], patches[3], patches[4],
        patches[5], patches[6], patches[7], patches[8], patches[9],
    ):
        up = client.post("/admin/community/uploads", files={"file": ("notes.md", b"student review text", "text/markdown")})
        assert up.status_code == 503

        ret = client.post(
            "/admin/community/review/CM-LIVE-1",
            json={"action": "retract", "retraction_reason": "Emergency retraction"},
        )
        assert ret.status_code == 200
        assert ret.json()["record"]["moderation_status"] == "RETRACTED"


def test_comm004_finding7_mime_extension_mismatch_and_binary_signatures_rejected_with_4xx():
    store = _InMemoryCommunityStore()
    client = TestClient(_build_app())
    patches = _patch_store(store)
    with (
        patch("app.routers.community_admin.get_config", return_value=_config(ingestion_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(ingestion_enabled=True)),
        patches[0], patches[1], patches[2], patches[3], patches[4],
        patches[5], patches[6], patches[7], patches[8], patches[9],
    ):
        # Mismatched MIME type (.json with application/pdf)
        r1 = client.post("/admin/community/uploads", files={"file": ("data.json", b'{"a":1}', "application/pdf")})
        assert r1.status_code == 415

        # Binary PDF signature disguised as .txt with text/plain
        r2 = client.post("/admin/community/uploads", files={"file": ("fake.txt", b"%PDF-1.7 binary content", "text/plain")})
        assert r2.status_code == 415

        # HTML disguised as .md
        r3 = client.post("/admin/community/uploads", files={"file": ("page.md", b"<!doctype html><html><body>hi</body></html>", "text/markdown")})
        assert r3.status_code == 415

        # Non-JSON text with .json extension and application/json MIME
        r4 = client.post("/admin/community/uploads", files={"file": ("bad.json", b"plain text not json", "application/json")})
        assert r4.status_code == 400


def test_h3_rejects_oversized_1_1_mib_upload_before_durable_writes():
    store = _InMemoryCommunityStore()
    client = TestClient(_build_app())
    oversized = b"a" * (1100 * 1024)
    patches = _patch_store(store)
    with (
        patch("app.routers.community_admin.get_config", return_value=_config(ingestion_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(ingestion_enabled=True)),
        patches[0], patches[1], patches[2], patches[3], patches[4],
        patches[5], patches[6], patches[7], patches[8], patches[9],
    ):
        res_big = client.post("/admin/community/uploads", files={"file": ("big.md", oversized, "text/markdown")})
        assert res_big.status_code == 413
        assert len(store.archives) == 0
        assert len(store.jobs) == 0


def test_comm004_finding4_interrupted_ingestion_resumes_and_completes_instead_of_false_duplicate():
    """Interrupted mid-job upload must resume on retry and only report duplicate once all records succeed."""
    store = _InMemoryCommunityStore(fail_on_record_index=2)
    client = TestClient(_build_app())
    payload = (
        json.dumps({"community_id": "CM-RES-1", "topic_ar": "موضوع أول", "summary_sanitized_ar": "تجربة أولى"}) + "\n" +
        json.dumps({"community_id": "CM-RES-2", "topic_ar": "موضوع ثاني", "summary_sanitized_ar": "تجربة ثانية"})
    ).encode("utf-8")

    patches = _patch_store(store)
    with (
        patch("app.routers.community_admin.get_config", return_value=_config(ingestion_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(ingestion_enabled=True)),
        patches[0], patches[1], patches[2], patches[3], patches[4],
        patches[5], patches[6], patches[7], patches[8], patches[9],
    ):
        # Attempt 1 fails on 2nd record -> job marked FAILED with only CM-RES-1 written.
        first = client.post("/admin/community/uploads", files={"file": ("batch.jsonl", payload, "application/x-ndjson")})
        assert first.status_code == 500
        assert set(store.records.keys()) == {"CM-RES-1"}

        # Attempt 2 resumes the FAILED job, skips CM-RES-1 safely, creates CM-RES-2, completes QUARANTINED.
        second = client.post("/admin/community/uploads", files={"file": ("batch.jsonl", payload, "application/x-ndjson")})
        assert second.status_code == 202
        assert second.json()["duplicate"] is False
        assert set(store.records.keys()) == {"CM-RES-1", "CM-RES-2"}
        assert second.json()["job"]["status"] == "QUARANTINED"

        # Attempt 3 now sees completed verified record set -> duplicate=True.
        third = client.post("/admin/community/uploads", files={"file": ("batch.jsonl", payload, "application/x-ndjson")})
        assert third.status_code == 202
        assert third.json()["duplicate"] is True


def test_comm004_finding6_approval_requires_server_verified_independent_evidence_artifact():
    """Approval must load immutable evidence artifact, recompute SHA-256 server-side, reject forged hashes & self-attestation."""
    store = _InMemoryCommunityStore()
    verifier_client = TestClient(_build_app(admin_uid="verifier-uid-2"))
    reviewer_client = TestClient(_build_app(admin_uid="reviewer-uid-1"))

    jsonl_line = json.dumps({
        "community_id": "CM-EV-99",
        "dedup_group_id": "DG-EV-99",
        "category": "studying",
        "content_nature": "STUDENT_EXPERIENCE_GUIDANCE",
        "topic_ar": "نصائح طلابية للمراجعة الليلية",
        "summary_sanitized_ar": "تجربة طلابية عامة لمتابعة الشواغر.",
        "identifiable_person_mentioned": False,
        "defamation_or_sensitivity_flag": False,
    }, ensure_ascii=False).encode("utf-8")

    patches = _patch_store(store)
    with (
        patch("app.routers.community_admin.get_config", return_value=_config(ingestion_enabled=True)),
        patch("app.services.community_service.get_config", return_value=_config(ingestion_enabled=True)),
        patches[0], patches[1], patches[2], patches[3], patches[4],
        patches[5], patches[6], patches[7], patches[8], patches[9],
    ):
        up = reviewer_client.post("/admin/community/uploads", files={"file": ("items.jsonl", jsonl_line, "application/x-ndjson")})
        assert up.status_code == 202

        # Register immutable evidence artifact by an INDEPENDENT verifier (verifier-uid-2).
        ev_res = verifier_client.post(
            "/admin/community/evidence",
            json={
                "canonical_source_url": "https://x.com/ku_students/status/1859990055",
                "captured_publisher": "@ku_students",
                "captured_at": "2026-11-25T15:55:00Z",
                "publication_date": "2026-11-20",
                "captured_snapshot_text": "منشور عام أصلي حول متابعة الشواغر الليلية.",
            },
        )
        assert ev_res.status_code == 201
        ev = ev_res.json()["evidence"]
        evidence_id = ev["evidence_id"]
        real_hash = ev["computed_sha256"]

        # 1. Forged syntactically valid SHA-256 hash is rejected (422).
        forged = reviewer_client.post(
            "/admin/community/review/CM-EV-99",
            json={
                "action": "approve",
                "expected_version": 1,
                "evidence_id": evidence_id,
                "evidence_hash": "f" * 64,
                "provenance_status": "SOURCE_VERIFIED",
                "privacy_review_status": "PASS",
                "rights_review_status": "PASS",
            },
        )
        assert forged.status_code == 422
        assert "EVIDENCE_DIGEST_MISMATCH" in forged.json()["detail"]["reasons"]

        # 2. Self-attestation (verifier-uid-2 trying to approve their own captured evidence) is rejected (422).
        self_att = verifier_client.post(
            "/admin/community/review/CM-EV-99",
            json={
                "action": "approve",
                "expected_version": 1,
                "evidence_id": evidence_id,
                "evidence_hash": real_hash,
                "provenance_status": "SOURCE_VERIFIED",
                "privacy_review_status": "PASS",
                "rights_review_status": "PASS",
            },
        )
        assert self_att.status_code == 422
        assert "SELF_ATTESTED_EVIDENCE_FORBIDDEN" in self_att.json()["detail"]["reasons"]

        # 3. Independent reviewer (reviewer-uid-1) with verified artifact succeeds (200) and produces v2 attestation.
        ok = reviewer_client.post(
            "/admin/community/review/CM-EV-99",
            json={
                "action": "approve",
                "expected_version": 1,
                "evidence_id": evidence_id,
                "evidence_hash": real_hash,
                "provenance_status": "SOURCE_VERIFIED",
                "privacy_review_status": "PASS",
                "rights_review_status": "PASS",
            },
        )
        assert ok.status_code == 200
        rec = ok.json()["record"]
        assert rec["moderation_status"] == "APPROVED_FOR_COMMUNITY_PUBLICATION"
        assert rec["approval_attestation"]["digest_version"] == "v2"
        assert rec["approval_attestation"]["evidence_id"] == evidence_id
        assert rec["approval_attestation"]["evidence_captured_by"] == "verifier-uid-2"
