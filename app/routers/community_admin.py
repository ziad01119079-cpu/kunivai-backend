"""Admin endpoints for Community Knowledge ingestion, evidence capture, review, and retraction.

Strictly isolated from existing Official Knowledge `/upload` endpoints.
Enforces H3, H4, M3, COMM-004 Finding 6 & Finding 7:
- Bounded upload cap (256 KiB, safely under Firestore's 1 MiB document cap).
- Strict extension/MIME agreement and binary/HTML content-signature validation (4xx on mismatch).
- Immutable server-hashed evidence artifact registration (`POST /admin/community/evidence`).
- UID-scoped write rate limiting and bounded concurrency semaphore.
- Retraction (`action="retract"`) remains accessible to authorized admins even
  when `COMMUNITY_INGESTION_ENABLED=false` so harmful content can always be revoked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from ..auth import verify_admin
from ..community_repository import (
    CommunityConcurrencyError,
    CommunityIdCollisionError,
    CommunityIngestionInProgressError,
)
from ..config import get_config
from ..services.community_models import (
    CommunityReviewActionRequest,
    EvidenceArtifactCreateRequest,
)
from ..services.community_service import (
    CommunityMonotonicRetractionError,
    CommunityPublicationDeniedError,
    CommunityValidationError,
    apply_review_action,
    get_ingestion_job_status,
    get_review_queue,
    ingest_community_upload,
    register_evidence_artifact,
)
from ..services.request_coalescer import chat_request_coalescer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/community", tags=["community-admin"])

# COMM-004 Finding 7: Explicit extension-to-allowed-MIME agreement map.
ALLOWED_MIMES_BY_EXTENSION: dict[str, frozenset[str]] = {
    ".md": frozenset({"text/markdown", "text/x-markdown", "text/plain"}),
    ".txt": frozenset({"text/plain"}),
    ".json": frozenset({"application/json", "text/json"}),
    ".jsonl": frozenset({"application/x-ndjson", "application/ndjson", "application/jsonl", "application/json", "text/plain"}),
}

_BINARY_SIGNATURES: tuple[bytes, ...] = (
    b"%PDF-",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"PK\x03\x04",
    b"\x1f\x8b\x08",
    b"MZ",
    b"\x7fELF",
)

# H3: 256 KiB cap keeps every parsed upload and metadata document safely under Firestore 1 MiB limits.
MAX_COMMUNITY_FILE_SIZE = 256 * 1024

# M3: UID-scoped write rate limit + bounded concurrency for admin community writes.
_ADMIN_WRITE_WINDOW_SECONDS = 60.0
_ADMIN_WRITE_MAX_REQUESTS = 30
_admin_write_windows: dict[str, deque[float]] = {}
_admin_upload_semaphore = asyncio.Semaphore(4)


def reset_admin_community_rate_limits() -> None:
    """Test helper to clear in-memory admin write rate windows."""
    _admin_write_windows.clear()


def _enforce_admin_write_rate_limit(uid: str) -> None:
    now = time.monotonic()
    bucket = _admin_write_windows.setdefault(uid, deque())
    cutoff = now - _ADMIN_WRITE_WINDOW_SECONDS
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _ADMIN_WRITE_MAX_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Admin Community write rate limit exceeded. Try again shortly.",
        )
    bucket.append(now)


def _require_community_ingestion_enabled() -> None:
    config = get_config()
    if not getattr(config, "COMMUNITY_INGESTION_ENABLED", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Community Knowledge ingestion is disabled (COMMUNITY_INGESTION_ENABLED=false).",
        )


def _validate_upload_mime_and_signature(ext: str, content_type: str | None, raw_bytes: bytes) -> str:
    """Enforce extension/MIME agreement and reject binary or HTML-masquerading payloads (Finding 7)."""
    normalized_mime = (content_type or "").split(";", 1)[0].strip().lower()
    allowed_mimes = ALLOWED_MIMES_BY_EXTENSION[ext]
    if not normalized_mime or normalized_mime not in allowed_mimes:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"MIME type '{normalized_mime or 'missing'}' does not match extension '{ext}'. "
                f"Allowed: {', '.join(sorted(allowed_mimes))}"
            ),
        )

    if b"\x00" in raw_bytes or any(raw_bytes.startswith(sig) for sig in _BINARY_SIGNATURES):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Binary or non-text payload signature rejected for Community text upload.",
        )

    head_stripped = raw_bytes[:512].lstrip().lower()
    if head_stripped.startswith((b"<!doctype html", b"<html", b"<script", b"<?xml")):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="HTML/XML markup payload rejected for Community upload.",
        )

    if ext in {".json", ".jsonl"} and not head_stripped.startswith((b"{", b"[")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payload content does not match JSON structure for '{ext}'.",
        )

    return normalized_mime


@router.post("/evidence", status_code=status.HTTP_201_CREATED)
async def create_community_evidence(
    body: EvidenceArtifactCreateRequest,
    uid: str = Depends(verify_admin),
):
    """Register an immutable independently captured evidence artifact with server-computed SHA-256."""
    _require_community_ingestion_enabled()
    _enforce_admin_write_rate_limit(uid)
    try:
        artifact = await register_evidence_artifact(request=body, captured_by=uid)
    except CommunityValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except CommunityIdCollisionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return {"status": "created", "evidence": artifact}


@router.post("/uploads", status_code=status.HTTP_202_ACCEPTED)
async def upload_community_source(
    file: UploadFile = File(..., description="Community source file (.md, .txt, .json, .jsonl)"),
    uid: str = Depends(verify_admin),
):
    """Upload a bounded community source file into restricted quarantine (never auto-published)."""
    _require_community_ingestion_enabled()
    _enforce_admin_write_rate_limit(uid)

    if not file or not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No file provided.",
        )

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_MIMES_BY_EXTENSION:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file extension '{ext}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_MIMES_BY_EXTENSION.keys()))}"
            ),
        )

    chunks: list[bytes] = []
    total_size = 0
    while chunk := await file.read(64 * 1024):
        total_size += len(chunk)
        if total_size > MAX_COMMUNITY_FILE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Community file exceeds the maximum allowed size of {MAX_COMMUNITY_FILE_SIZE // 1024}KiB.",
            )
        chunks.append(chunk)

    if total_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    raw_bytes = b"".join(chunks)
    validated_mime = _validate_upload_mime_and_signature(ext, file.content_type, raw_bytes)

    async with _admin_upload_semaphore:
        try:
            job, is_duplicate = await ingest_community_upload(
                filename=Path(file.filename).name,
                content_type=validated_mime,
                raw_bytes=raw_bytes,
                uploaded_by=uid,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, CommunityValidationError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid community source payload: {exc}",
            ) from exc
        except (CommunityIdCollisionError, CommunityIngestionInProgressError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            logger.exception("Community upload ingestion failed for admin %s", uid)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Community ingestion failed.",
            ) from exc

    return {
        "duplicate": is_duplicate,
        "isolated_from_official_kb": True,
        "published_count": 0,
        "job": job,
    }


@router.get("/jobs/{job_id}")
async def get_community_job(
    job_id: str,
    uid: str = Depends(verify_admin),
):
    """Inspect a community ingestion job status."""
    _require_community_ingestion_enabled()
    job = await get_ingestion_job_status(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Community ingestion job '{job_id}' not found.",
        )
    return job


@router.get("/review")
async def list_community_review_queue(
    moderation_status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    uid: str = Depends(verify_admin),
):
    """List community records with provenance and publication eligibility status."""
    _require_community_ingestion_enabled()
    items = await get_review_queue(moderation_status=moderation_status, limit=limit)
    return {
        "count": len(items),
        "items": items,
    }


@router.post("/review/{community_id}")
async def review_community_record(
    community_id: str,
    body: CommunityReviewActionRequest,
    uid: str = Depends(verify_admin),
):
    """Approve, reject, or retract a community record (retraction always remains available)."""
    # M3: Retraction works even when COMMUNITY_INGESTION_ENABLED=false.
    if body.action != "retract":
        _require_community_ingestion_enabled()
    _enforce_admin_write_rate_limit(uid)

    try:
        updated = await apply_review_action(
            community_id=community_id,
            reviewer_uid=uid,
            request=body,
        )
    except CommunityPublicationDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "COMMUNITY_PUBLICATION_DENIED",
                "reasons": exc.reasons,
            },
        ) from exc
    except (CommunityConcurrencyError, CommunityMonotonicRetractionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Community record '{community_id}' not found.",
        )

    if body.action == "retract":
        chat_request_coalescer.invalidate_community_replay(community_id)

    return {
        "status": "updated",
        "action": body.action,
        "record": updated,
    }
