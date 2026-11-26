"""Isolated Firestore repository for Community Knowledge (H1, H2, H3, H4, H5, H6, M1, COMM-006).

Collections:
- community_source_archives/{source_sha256} (bounded metadata pointer only; no multi-MiB raw body)
- community_ingestion_jobs/{source_sha256}  (monotonic attempt_fence + lease-owned resumable job)
- community_records/{community_id}          (fenced create-only on ingest; transactional version-checked review)
- community_audit_logs/{auto_id}            (written atomically alongside moderation state transitions)
- community_revocations/{community_id}      (authoritative retraction markers checked across replicas)
- community_evidence_artifacts/{evidence_id} (immutable server-verified source capture artifacts)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from firebase_admin import firestore_async
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from .services.community_policy import evaluate_for_publication
from .services.coordination_runtime import distributed_enabled, get_redis_runtime

logger = logging.getLogger(__name__)

ARCHIVES_COLLECTION = "community_source_archives"
JOBS_COLLECTION = "community_ingestion_jobs"
RECORDS_COLLECTION = "community_records"
AUDIT_COLLECTION = "community_audit_logs"
REVOCATIONS_COLLECTION = "community_revocations"
EVIDENCE_COLLECTION = "community_evidence_artifacts"

_CACHE_TTL_SECONDS = 30.0
_published_cache: dict[str, Any] = {
    "records": None,
    "expires_at": 0.0,
}


class CommunityConcurrencyError(RuntimeError):
    """Raised on optimistic-version, stale attempt_fence, or monotonic invariant violation."""


class CommunityIdCollisionError(RuntimeError):
    """Raised when a distinct upload tries to overwrite an existing community_id."""


class CommunityIngestionInProgressError(RuntimeError):
    """Raised when an identical SHA-256 upload is currently actively leased by another worker."""


def invalidate_community_cache() -> None:
    """Immediately clear the local in-process published community record cache."""
    _published_cache["records"] = None
    _published_cache["expires_at"] = 0.0


async def _publish_distributed_revocation(community_id: str, approval_version: int) -> None:
    """Propagate retraction/invalidation to Redis when distributed coordination is active (H2)."""
    if not distributed_enabled():
        return
    try:
        runtime = get_redis_runtime()
        key = f"{runtime.requests.prefix}:community:revoked"
        await runtime.client.hset(key, community_id, str(approval_version))
    except Exception as exc:
        logger.warning("Redis community revocation broadcast failed (%s)", type(exc).__name__)


async def _is_revoked_in_redis(community_id: str, approval_version: int) -> bool | None:
    """Check Redis revocation marker if distributed coordination is enabled."""
    if not distributed_enabled():
        return None
    try:
        runtime = get_redis_runtime()
        key = f"{runtime.requests.prefix}:community:revoked"
        raw = await runtime.client.hget(key, community_id)
        if raw is None:
            return False
        revoked_ver = int(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        return revoked_ver >= approval_version
    except Exception:
        return True


async def create_evidence_artifact_immutable(artifact_data: dict[str, Any]) -> dict[str, Any]:
    """Create an immutable server-verified evidence artifact (create-only)."""
    db = firestore_async.client()
    evidence_id = str(artifact_data["evidence_id"])
    doc_ref = db.collection(EVIDENCE_COLLECTION).document(evidence_id)
    try:
        await doc_ref.create(artifact_data)
    except AlreadyExists as exc:
        raise CommunityIdCollisionError(f"Evidence artifact '{evidence_id}' already exists.") from exc
    return artifact_data


async def get_evidence_artifact(evidence_id: str) -> dict[str, Any] | None:
    """Load an immutable evidence artifact by evidence_id."""
    db = firestore_async.client()
    snap = await db.collection(EVIDENCE_COLLECTION).document(evidence_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


async def save_source_archive_idempotent(archive_data: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Persist bounded restricted archive metadata keyed by source_sha256."""
    db = firestore_async.client()
    sha256 = str(archive_data["source_sha256"])
    doc_ref = db.collection(ARCHIVES_COLLECTION).document(sha256)
    try:
        await doc_ref.create(archive_data)
        return True, archive_data
    except AlreadyExists:
        snap = await doc_ref.get()
        existing = snap.to_dict() if snap.exists else archive_data
        return False, existing or archive_data


async def claim_or_resume_ingestion_job(
    job_data: dict[str, Any],
    *,
    now_epoch: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Atomically claim a new SHA-256 job with monotonic attempt_fence, verify durable duplicate,
    or reclaim a FAILED / lease-expired job with `attempt_fence = current + 1` (H1).
    """
    db = firestore_async.client()
    sha256 = str(job_data["source_sha256"])
    doc_ref = db.collection(JOBS_COLLECTION).document(sha256)
    now = now_epoch if now_epoch is not None else time.time()
    incoming_expected = list(job_data.get("expected_record_ids") or [])

    @firestore.async_transactional
    async def _claim(transaction):
        snap = await doc_ref.get(transaction=transaction)
        if not snap.exists:
            claimed = {**job_data, "attempt_fence": 1}
            transaction.create(doc_ref, claimed)
            return "claimed", claimed

        existing = snap.to_dict() or {}
        status = existing.get("status")
        stored_expected = list(existing.get("expected_record_ids") or [])
        written = list(existing.get("record_ids") or [])
        current_fence = int(existing.get("attempt_fence") or 1)

        if (
            status == "QUARANTINED"
            and stored_expected
            and stored_expected == incoming_expected
            and set(written) == set(incoming_expected)
            and len(written) == len(incoming_expected)
        ):
            # H1: Verify every expected record actually exists durably with matching source_sha256.
            all_durable = True
            for cid in incoming_expected:
                rec_snap = await db.collection(RECORDS_COLLECTION).document(cid).get(transaction=transaction)
                if not rec_snap.exists:
                    all_durable = False
                    break
                rec_data = rec_snap.to_dict() or {}
                if rec_data.get("source_sha256") != sha256:
                    all_durable = False
                    break
            if all_durable:
                return "duplicate_completed", existing

        lease_expires = float(existing.get("lease_expires_at") or 0.0)
        if status == "CLAIMED" and lease_expires > now:
            raise CommunityIngestionInProgressError(
                f"Ingestion job for SHA '{sha256[:12]}' is currently in progress (fence={current_fence})."
            )

        next_fence = current_fence + 1
        resumed = {
            **existing,
            "status": "CLAIMED",
            "attempt_fence": next_fence,
            "attempt_id": job_data.get("attempt_id"),
            "lease_expires_at": job_data.get("lease_expires_at"),
            "expected_record_ids": incoming_expected or stored_expected,
            "updated_at": job_data.get("updated_at"),
            "error": None,
        }
        transaction.set(doc_ref, resumed)
        return "resumed", resumed

    return await _claim(db.transaction())


async def update_ingestion_job_fenced(
    source_sha256: str,
    *,
    attempt_fence: int,
    job_data: dict[str, Any],
) -> dict[str, Any]:
    """Atomically update/finalize an ingestion job ONLY if `attempt_fence` matches active CLAIMED owner (H1)."""
    db = firestore_async.client()
    doc_ref = db.collection(JOBS_COLLECTION).document(source_sha256)

    @firestore.async_transactional
    async def _update(transaction):
        snap = await doc_ref.get(transaction=transaction)
        if not snap.exists:
            raise CommunityConcurrencyError(f"Ingestion job '{source_sha256[:12]}' does not exist.")
        current = snap.to_dict() or {}
        cur_fence = int(current.get("attempt_fence") or 0)
        cur_status = current.get("status")
        if cur_status == "QUARANTINED":
            raise CommunityConcurrencyError(
                f"Cannot overwrite terminal QUARANTINED job '{source_sha256[:12]}' from stale fence={attempt_fence}."
            )
        if cur_status != "CLAIMED" or cur_fence != attempt_fence:
            raise CommunityConcurrencyError(
                f"Stale ingestion attempt fence={attempt_fence} (current fence={cur_fence}, status={cur_status})."
            )
        updated = {**job_data, "attempt_fence": attempt_fence}
        transaction.set(doc_ref, updated)
        return updated

    return await _update(db.transaction())


async def update_ingestion_job(source_sha256: str, job_data: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible wrapper requiring valid attempt_fence."""
    fence = int(job_data.get("attempt_fence") or 1)
    return await update_ingestion_job_fenced(source_sha256, attempt_fence=fence, job_data=job_data)


async def get_ingestion_job(job_id: str) -> dict[str, Any] | None:
    """Fetch an ingestion job by job_id or source_sha256."""
    db = firestore_async.client()
    snap = await db.collection(JOBS_COLLECTION).document(job_id).get()
    if snap.exists:
        return snap.to_dict()
    query = db.collection(JOBS_COLLECTION).where("job_id", "==", job_id).limit(1)
    async for doc in query.stream():
        data = doc.to_dict()
        if data:
            return data
    return None


async def create_community_record_fenced(
    record_data: dict[str, Any],
    *,
    source_sha256: str,
    attempt_fence: int,
    allow_same_job_resume: bool = False,
) -> dict[str, Any]:
    """Atomically verify active ingestion `attempt_fence` before creating a quarantined record (H1)."""
    db = firestore_async.client()
    community_id = str(record_data["community_id"])
    job_ref = db.collection(JOBS_COLLECTION).document(source_sha256)
    rec_ref = db.collection(RECORDS_COLLECTION).document(community_id)

    @firestore.async_transactional
    async def _create(transaction):
        job_snap = await job_ref.get(transaction=transaction)
        if not job_snap.exists:
            raise CommunityConcurrencyError("Ingestion job missing during fenced record write.")
        job = job_snap.to_dict() or {}
        if job.get("status") != "CLAIMED" or int(job.get("attempt_fence") or 0) != attempt_fence:
            raise CommunityConcurrencyError(
                f"Stale ingestion attempt fence={attempt_fence} cannot write record '{community_id}'."
            )

        rec_snap = await rec_ref.get(transaction=transaction)
        if rec_snap.exists:
            existing = rec_snap.to_dict() or {}
            if (
                allow_same_job_resume
                and existing.get("source_sha256") == source_sha256
                and existing.get("ingestion_job_id") == record_data.get("ingestion_job_id")
                and existing.get("moderation_status") != "APPROVED_FOR_COMMUNITY_PUBLICATION"
                and existing.get("index_namespace") == "COMMUNITY_CANDIDATE_ONLY"
            ):
                return existing
            raise CommunityIdCollisionError(
                f"Community record '{community_id}' already exists and cannot be overwritten by ingestion."
            )
        transaction.create(rec_ref, record_data)
        return dict(record_data)

    result = await _create(db.transaction())
    invalidate_community_cache()
    return result


async def create_community_record_strict(
    record_data: dict[str, Any],
    *,
    allow_same_job_resume: bool = False,
) -> dict[str, Any]:
    """Create a quarantined community record without ever overwriting another upload's or published record."""
    db = firestore_async.client()
    community_id = str(record_data["community_id"])
    doc_ref = db.collection(RECORDS_COLLECTION).document(community_id)
    try:
        await doc_ref.create(record_data)
    except AlreadyExists as exc:
        if allow_same_job_resume:
            snap = await doc_ref.get()
            existing = snap.to_dict() if snap.exists else None
            if (
                existing
                and existing.get("source_sha256") == record_data.get("source_sha256")
                and existing.get("ingestion_job_id") == record_data.get("ingestion_job_id")
                and existing.get("moderation_status") != "APPROVED_FOR_COMMUNITY_PUBLICATION"
                and existing.get("index_namespace") == "COMMUNITY_CANDIDATE_ONLY"
            ):
                return existing
        raise CommunityIdCollisionError(
            f"Community record '{community_id}' already exists and cannot be overwritten by ingestion."
        ) from exc
    invalidate_community_cache()
    return record_data


async def get_community_record(community_id: str) -> dict[str, Any] | None:
    """Fetch a community record by community_id."""
    db = firestore_async.client()
    snap = await db.collection(RECORDS_COLLECTION).document(community_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


async def list_community_records(
    *,
    moderation_status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List community records for admin review, optionally filtered by status."""
    db = firestore_async.client()
    col_ref = db.collection(RECORDS_COLLECTION)
    if moderation_status:
        query = col_ref.where("moderation_status", "==", moderation_status).limit(limit)
    else:
        query = col_ref.limit(limit)

    items: list[dict[str, Any]] = []
    async for snap in query.stream():
        data = snap.to_dict()
        if data:
            items.append(data)
    return items


async def transition_community_record_atomic(
    community_id: str,
    *,
    expected_version: int,
    new_record: dict[str, Any],
    audit_entry: dict[str, Any],
    retraction_marker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically validate version, approval_version monotonicity, and commit moderation, audit, and revocation."""
    db = firestore_async.client()
    rec_ref = db.collection(RECORDS_COLLECTION).document(community_id)
    audit_ref = db.collection(AUDIT_COLLECTION).document()
    rev_ref = db.collection(REVOCATIONS_COLLECTION).document(community_id)

    @firestore.async_transactional
    async def _commit(transaction):
        snap = await rec_ref.get(transaction=transaction)
        if not snap.exists:
            raise KeyError(community_id)
        current = snap.to_dict() or {}
        current_version = int(current.get("record_version") or 1)
        if current_version != expected_version:
            raise CommunityConcurrencyError(
                f"Concurrent modification for '{community_id}': "
                f"expected version {expected_version}, found {current_version}."
            )
        if new_record.get("community_id") != community_id:
            raise CommunityConcurrencyError("Moderation cannot change community_id.")
        if type(new_record.get("record_version")) is not int or new_record["record_version"] != current_version + 1:
            raise CommunityConcurrencyError("record_version must increment by exactly one.")
        old_status = current.get("moderation_status")
        next_status = new_record.get("moderation_status")
        if (
            audit_entry.get("community_id") != community_id
            or audit_entry.get("previous_version") != current_version
            or audit_entry.get("new_version") != current_version + 1
            or audit_entry.get("previous_status") != old_status
            or audit_entry.get("new_status") != next_status
        ):
            raise CommunityConcurrencyError("Audit record does not match current state.")

        previous_approval = current.get("approval_version")
        proposed_approval = new_record.get("approval_version")
        if type(previous_approval) is not int or type(proposed_approval) is not int:
            raise CommunityConcurrencyError("approval_version must be an integer on every transition.")
        if next_status == "APPROVED_FOR_COMMUNITY_PUBLICATION":
            if proposed_approval != previous_approval + 1:
                raise CommunityConcurrencyError(
                    "Every approval must increment the authoritative approval_version exactly once."
                )
        else:
            if proposed_approval != previous_approval:
                raise CommunityConcurrencyError(
                    "Non-approval transitions must preserve approval_version unchanged."
                )

        if old_status == "RETRACTED" and next_status != "RETRACTED":
            if (
                next_status != "APPROVED_FOR_COMMUNITY_PUBLICATION"
                or audit_entry.get("reapprove_after_retraction") is not True
                or proposed_approval <= previous_approval
            ):
                raise CommunityConcurrencyError(
                    "Retracted records require explicitly audited, versioned reapproval."
                )
        if next_status == "RETRACTED" and retraction_marker is None:
            raise CommunityConcurrencyError("Retraction requires an authoritative marker.")
        if retraction_marker is not None:
            if next_status != "RETRACTED" or retraction_marker.get("community_id") != community_id:
                raise CommunityConcurrencyError("Revocation marker does not match record.")
            if int(retraction_marker.get("revoked_approval_version") or 0) < max(1, previous_approval):
                raise CommunityConcurrencyError("Revocation marker does not cover current approval.")

        transaction.set(rec_ref, new_record)
        transaction.create(audit_ref, audit_entry)
        if retraction_marker is not None:
            transaction.set(rev_ref, retraction_marker)
        return dict(new_record)

    result = await _commit(db.transaction())
    invalidate_community_cache()
    if retraction_marker is not None:
        await _publish_distributed_revocation(
            community_id, int(retraction_marker.get("revoked_approval_version") or 0)
        )
    return result


async def verify_record_not_revoked(community_id: str, approval_version: int) -> bool:
    """Authoritative delivery/replay check against Redis + Firestore + full v3 digest + expiry + evidence (H2, H5).

    Returns True ONLY if:
    - Not revoked in Redis or `community_revocations`
    - Current record exists at exact `approval_version`
    - Current record passes FULL `evaluate_for_publication(..., require_published_namespace=True)`
      (validating current v3 approval digest, attestation fields, expiry, and PII/defamation)
    - Linked immutable evidence artifact still exists in `community_evidence_artifacts`
    Fails closed (returns False) on any error.
    """
    if type(approval_version) is not int or approval_version < 1:
        return False

    redis_revoked = await _is_revoked_in_redis(community_id, approval_version)
    if redis_revoked is True:
        return False

    try:
        db = firestore_async.client()
        rev_snap = await db.collection(REVOCATIONS_COLLECTION).document(community_id).get()
        if rev_snap.exists:
            rev_data = rev_snap.to_dict() or {}
            if int(rev_data.get("revoked_approval_version") or 0) >= approval_version:
                return False

        rec_snap = await db.collection(RECORDS_COLLECTION).document(community_id).get()
        if not rec_snap.exists:
            return False
        rec = rec_snap.to_dict() or {}
        if type(rec.get("approval_version")) is not int or rec["approval_version"] != approval_version:
            return False

        # H5: Re-validate FULL v3 digest, attestation fields, namespace, and expiry against live DB record!
        decision = evaluate_for_publication(rec, require_published_namespace=True)
        if not decision.allowed:
            return False

        # H5: Verify linked immutable evidence artifact is still available.
        ev_id = str(rec.get("evidence_id") or "").strip()
        if not ev_id:
            return False
        ev_snap = await db.collection(EVIDENCE_COLLECTION).document(ev_id).get()
        if not ev_snap.exists:
            return False
        ev_data = ev_snap.to_dict() or {}
        if ev_data.get("computed_sha256") != rec.get("evidence_hash"):
            return False

        return True
    except Exception as exc:
        logger.warning("Authoritative revocation check failed closed for %s (%s)", community_id, type(exc).__name__)
        return False


async def query_published_community_records(
    *,
    limit: int = 100,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Load candidate published records (bounded TTL cache).

    M1: Ranking happens BEFORE bounded authoritative re-verification of top-K matches
    in `community_service.py` (which always calls `verify_records_still_published` on
    every selected record before use). On cache hit, still drop any locally/Redis revoked items.
    """
    now = time.monotonic()
    if use_cache and _published_cache["records"] is not None and now < _published_cache["expires_at"]:
        return list(_published_cache["records"][:limit])

    db = firestore_async.client()
    query = (
        db.collection(RECORDS_COLLECTION)
        .where("moderation_status", "==", "APPROVED_FOR_COMMUNITY_PUBLICATION")
        .where("isolated_from_official_kb", "==", True)
        .where("index_namespace", "==", "COMMUNITY_PUBLISHED")
        .limit(limit)
    )
    results: list[dict[str, Any]] = []
    async for snap in query.stream():
        data = snap.to_dict()
        if data:
            results.append(data)

    if use_cache:
        _published_cache["records"] = list(results)
        _published_cache["expires_at"] = now + _CACHE_TTL_SECONDS
    return results
