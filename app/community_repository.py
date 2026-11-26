"""Isolated Firestore repository for Community Knowledge (H2, H3, H4, H6).

Uses dedicated top-level Firestore collections completely separate from:
- Official Knowledge (OPENAI_VECTOR_STORE_ID)
- Private user schedules (users/{uid}/schedules)
- Private user chat history (users/{uid}/chats)

Collections:
- community_source_archives/{source_sha256} (bounded metadata pointer only; no multi-MiB raw body)
- community_ingestion_jobs/{source_sha256}  (atomically claimed per SHA-256 via .create())
- community_records/{community_id}          (create-only on ingest; version-checked transactional review)
- community_audit_logs/{auto_id}            (written atomically alongside moderation state transitions)
- community_revocations/{community_id}      (authoritative retraction markers checked across replicas)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from firebase_admin import firestore_async
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from .services.coordination_runtime import distributed_enabled, get_redis_runtime

logger = logging.getLogger(__name__)

ARCHIVES_COLLECTION = "community_source_archives"
JOBS_COLLECTION = "community_ingestion_jobs"
RECORDS_COLLECTION = "community_records"
AUDIT_COLLECTION = "community_audit_logs"
REVOCATIONS_COLLECTION = "community_revocations"

_CACHE_TTL_SECONDS = 30.0
_published_cache: dict[str, Any] = {
    "records": None,
    "expires_at": 0.0,
}


class CommunityConcurrencyError(RuntimeError):
    """Raised on optimistic-version mismatch during concurrent moderation."""


class CommunityIdCollisionError(RuntimeError):
    """Raised when a distinct upload tries to overwrite an existing community_id."""


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
        # Fail closed if Redis revocation state is unreadable while distributed mode is active.
        return True


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


async def claim_ingestion_job_atomic(job_data: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Atomically claim the full SHA-256 ingestion job BEFORE any record writes (H4)."""
    db = firestore_async.client()
    sha256 = str(job_data["source_sha256"])
    doc_ref = db.collection(JOBS_COLLECTION).document(sha256)
    try:
        await doc_ref.create(job_data)
        return True, job_data
    except AlreadyExists:
        snap = await doc_ref.get()
        existing = snap.to_dict() if snap.exists else job_data
        return False, existing or job_data


async def update_ingestion_job(source_sha256: str, job_data: dict[str, Any]) -> dict[str, Any]:
    """Update a claimed ingestion job with final extraction counts."""
    db = firestore_async.client()
    await db.collection(JOBS_COLLECTION).document(source_sha256).set(job_data)
    return job_data


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


async def create_community_record_strict(record_data: dict[str, Any]) -> dict[str, Any]:
    """Create a quarantined community record without ever overwriting an existing ID (H4)."""
    db = firestore_async.client()
    community_id = str(record_data["community_id"])
    doc_ref = db.collection(RECORDS_COLLECTION).document(community_id)
    try:
        await doc_ref.create(record_data)
    except AlreadyExists as exc:
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
    """Atomically validate version and commit moderation, audit, and revocation.

    The read and version check MUST occur inside the Firestore transaction.
    Concurrent writers are retried against the latest version.
    """
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
        if next_status == "APPROVED_FOR_COMMUNITY_PUBLICATION":
            previous_approval = current.get("approval_version")
            proposed_approval = new_record.get("approval_version")
            if (
                type(previous_approval) is not int
                or type(proposed_approval) is not int
                or proposed_approval != previous_approval + 1
            ):
                raise CommunityConcurrencyError(
                    "Every approval must increment the authoritative approval_version exactly once."
                )
        if old_status == "RETRACTED" and next_status != "RETRACTED":
            if (
                next_status != "APPROVED_FOR_COMMUNITY_PUBLICATION"
                or audit_entry.get("reapprove_after_retraction") is not True
                or int(new_record.get("approval_version") or 0)
                <= int(current.get("approval_version") or 0)
            ):
                raise CommunityConcurrencyError(
                    "Retracted records require explicitly audited, versioned reapproval."
                )
        if next_status == "RETRACTED" and retraction_marker is None:
            raise CommunityConcurrencyError("Retraction requires an authoritative marker.")
        if retraction_marker is not None:
            if next_status != "RETRACTED" or retraction_marker.get("community_id") != community_id:
                raise CommunityConcurrencyError("Revocation marker does not match record.")
            if int(retraction_marker.get("revoked_approval_version") or 0) < max(
                1, int(current.get("approval_version") or 0)
            ):
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
    """Authoritative revocation & version check against Redis + Firestore (H2).

    Returns True ONLY if the record is still published at `approval_version` and
    has not been retracted. Fails closed (returns False) on any lookup error.
    """
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
        if rec.get("moderation_status") != "APPROVED_FOR_COMMUNITY_PUBLICATION":
            return False
        if rec.get("index_namespace") != "COMMUNITY_PUBLISHED":
            return False
        if int(rec.get("approval_version") or 0) != approval_version:
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
    """Load records marked APPROVED_FOR_COMMUNITY_PUBLICATION in the isolated store.

    Cache hits are ALWAYS re-validated against authoritative revocation/version
    state before returning (H2). Callers MUST still apply `filter_retrieved_community()`.
    """
    now = time.monotonic()
    if use_cache and _published_cache["records"] is not None and now < _published_cache["expires_at"]:
        verified_cached: list[dict[str, Any]] = []
        for rec in _published_cache["records"][:limit]:
            cid = str(rec.get("community_id") or "")
            ver = int(rec.get("approval_version") or 0)
            if cid and ver >= 1 and await verify_record_not_revoked(cid, ver):
                verified_cached.append(rec)
        return verified_cached

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
