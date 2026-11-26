"""Service layer for Community Knowledge ingestion, moderation, and retrieval.

Enforces C1, H1-H6, M2-M3:
- Validates size, nesting depth, record count, and field limits BEFORE durable writes.
- Claims SHA-256 ingestion jobs atomically; creates records with create-only semantics
  so a second upload can never overwrite an existing/approved community_id.
- Keeps imported claims strictly separate from server-managed `approval_attestation`.
- Binds every approval to `approved_content_hash`, `approval_version`, `captured_publisher`,
  `captured_at`, and `evidence_hash`; commits state + audit + revocation atomically.
- Retraction is monotonic unless an explicit audited reapproval increments `approval_version`.
- Retrieval enforces bounded timeout, per-record schema safety, meaningful topic overlap
  (never category-only), authoritative revocation checks, and programmatic rendering.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from ..community_repository import (
    CommunityConcurrencyError,
    CommunityIdCollisionError,
    claim_ingestion_job_atomic,
    create_community_record_strict,
    get_community_record,
    get_ingestion_job,
    list_community_records,
    query_published_community_records,
    save_source_archive_idempotent,
    transition_community_record_atomic,
    update_ingestion_job,
    verify_record_not_revoked,
)
from ..config import get_config
from .community_models import (
    ApprovalAttestation,
    CommunityArchiveRecord,
    CommunityCategory,
    CommunityIngestionJob,
    CommunityRecord,
    CommunityRetrievalResult,
    CommunityReviewActionRequest,
    ContentNature,
    IndexNamespace,
    IngestionJobStatus,
    ModerationStatus,
    ProvenanceStatus,
    ReviewStatus,
)
from .community_policy import (
    Decision,
    compute_record_content_hash,
    detect_pii_or_defamation_signals,
    evaluate_for_publication,
    filter_retrieved_community,
    is_valid_iso_timestamp,
    is_valid_sha256_hex,
    sanitize_community_text,
)

logger = logging.getLogger(__name__)

MAX_RECORDS_PER_UPLOAD = 100
MAX_JSON_NESTING_DEPTH = 6
MAX_FIELD_LENGTH = 2000
RETRIEVAL_TIMEOUT_SECONDS = 3.0

FIXED_SEPARATION_DISCLAIMER_AR = (
    "رأي وتجربة طلابية شخصية مؤرّخة وغير رسمية؛ لا تمثل لائحة أو قراراً رسمياً من جامعة الكويت."
)
FIXED_SEPARATION_DISCLAIMER_EN = (
    "Subjective, dated student opinion/experience; not an official Kuwait University regulation or announcement."
)

_OFFICIAL_REGULATION_MARKERS = (
    r"(?:لائحة|لوائح|المادة\s*\d|قانون الجامعة|قرار جامعي)",
    r"(?:بايفورس|وحدة دراسية|وحدات التخرج|الحد الأقصى للوحدات|الإنذار الأكاديمي|الانذار الاكاديمي|تظلم|رفع تظلم)",
    r"(?:موعد التسجيل|يفتح السستم|يبطل السستم|وقف القيد|التحويل الداخلي|شروط القبول|فصل أكاديمي)",
    r"\b(?:official regulation|academic warning|bylaw|probation|gpa threshold|registration deadline|withdrawal deadline)\b",
)

_UNVERIFIED_FACT_MARKERS = (
    r"(?:الدور الأول|الدور الثاني|الدور الثالث|مبنى رقم|يفتح الساعة|يسكر الساعة|تستغرق شهر|بعد شهر)",
    r"\b(?:opens at|closes at|located on floor|takes \d+ weeks|office hours)\b",
)

_PERSON_MENTION_MARKERS = (
    r"(?:^|\s)(?:د\.|دكتور|الدكتور|الدكتورة|دكتورة|أستاذ|الأستاذ|استاذ|الاستاذ|بروفيسور)(?:\s|$)",
    r"\b(?:dr\.|prof\.|professor|instructor)\b",
)

_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CommunityCategory.PROFESSORS.value, ("دكتور", "الدكتور", "دكتورة", "أستاذ", "استاذ", "شعبة", "شعبتين", "professor", "instructor", "dr.")),
    (CommunityCategory.CAMPUS_DINING.value, ("مطعم", "كافيه", "كافتيريا", "قهوة", "أكل", "restaurant", "cafe", "coffee", "food", "dining")),
    (CommunityCategory.COURSES.value, ("مقرر", "مادة", "مواد", "ميدتيرم", "فاينل", "رفع المعدل", "اختياري", "course", "elective", "midterm")),
    (CommunityCategory.STUDYING.value, ("مذاكرة", "دراسة", "ملخص", "نصائح", "شواغر", "study", "tips", "notes")),
    (CommunityCategory.CAMPUS_SERVICES.value, ("مكتبة", "مواقف", "سستم", "توجيه وإرشاد", "السجلات", "الشهادة", "library", "parking", "campus")),
)

_ARABIC_NORM_TABLE = str.maketrans({
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ة": "ه",
    "ى": "ي",
    "ؤ": "و",
    "ئ": "ي",
})

# Stop words + generic intent/category words that must not create false cross-topic matches (M2).
_STOP_WORDS = frozenset({
    "في", "من", "على", "عن", "الى", "إلى", "هل", "ما", "شنو", "وش", "ايه", "إيه",
    "اللي", "الذي", "التي", "مع", "او", "أو", "هذا", "هذه", "مين", "منو", "ابغي", "أبغي",
    "راي", "رأي", "اراء", "آراء", "الطلاب", "الطلبه", "طلاب", "طلبه", "طلابي", "طلابيه",
    "دكتور", "الدكتور", "استاذ", "الاستاذ", "ماده", "مادة", "مقرر", "الجامعه", "جامعة", "بالجامعه",
    "افضل", "احسن", "اسوا", "اسوء", "تجربه", "تجارب", "تقييم", "تقييمات",
    "the", "is", "are", "what", "which", "best", "worst", "good", "about", "for", "at", "in",
    "of", "to", "student", "students", "opinion", "opinions", "review", "reviews", "professor", "course",
})


class CommunityValidationError(ValueError):
    """Raised when an uploaded payload violates structural or resource bounds (H3)."""


class CommunityPublicationDeniedError(ValueError):
    """Raised when an approval attempt fails one or more fail-closed policy gates (C1)."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("Community publication denied: " + ", ".join(reasons))


class CommunityMonotonicRetractionError(RuntimeError):
    """Raised when a stale/regular approval tries to override a retracted record (H4)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_search_text(text: str) -> str:
    text = re.sub(r"[\u064b-\u065f]", "", text.casefold())
    text = text.translate(_ARABIC_NORM_TABLE)
    text = re.sub(r"[؟،؛!?,.:;()\[\]\"'«»\-_/\\]+", " ", text)
    text = re.sub(r"[^\w\s]+", " ", text)
    return " ".join(text.split())


def _tokenize_topic(text: str) -> set[str]:
    tokens = set()
    for tok in _normalize_search_text(text).split():
        if len(tok) < 2:
            continue
        if tok in _STOP_WORDS:
            continue
        # Strip leading Arabic definite article 'ال' for consistent stem matching
        if tok.startswith("ال") and len(tok) > 4:
            stripped = tok[2:]
            if stripped not in _STOP_WORDS:
                tokens.add(stripped)
        tokens.add(tok)
    return tokens


def _check_json_depth(obj: Any, current: int = 1) -> None:
    if current > MAX_JSON_NESTING_DEPTH:
        raise CommunityValidationError(
            f"JSON nesting depth exceeds maximum allowed ({MAX_JSON_NESTING_DEPTH})."
        )
    if isinstance(obj, dict):
        for val in obj.values():
            _check_json_depth(val, current + 1)
    elif isinstance(obj, list):
        for item in obj:
            _check_json_depth(item, current + 1)


def _infer_category(text: str) -> str:
    norm = _normalize_search_text(text)
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(_normalize_search_text(kw) in norm for kw in keywords):
            return category
    return CommunityCategory.OTHER.value


def _classify_text_entry(
    raw_text: str,
    *,
    community_id: str,
    dedup_group_id: str,
    source_archive_path: str,
    source_sha256: str,
    ingestion_job_id: str,
    now_iso: str,
) -> CommunityRecord:
    """Classify an unstructured text/markdown block into a QUARANTINED CommunityRecord."""
    cleaned = sanitize_community_text(raw_text, max_length=700)
    first_line = sanitize_community_text(raw_text.splitlines()[0] if raw_text.splitlines() else cleaned, max_length=140)
    lowered = raw_text.casefold()

    identifiable_person = any(re.search(p, lowered) for p in _PERSON_MENTION_MARKERS)
    has_sensitive_signal, _ = detect_pii_or_defamation_signals(raw_text)
    category = _infer_category(raw_text)

    if any(re.search(p, lowered) for p in _OFFICIAL_REGULATION_MARKERS):
        content_nature = ContentNature.UNOFFICIAL_CLAIM_ABOUT_OFFICIAL_REGULATION.value
        moderation_status = ModerationStatus.EXCLUDED_MUST_USE_OFFICIAL_KB.value
    elif has_sensitive_signal:
        content_nature = ContentNature.PERSONAL_OPINION.value
        moderation_status = ModerationStatus.BLOCKED_SENSITIVE_DEFAMATION_REVIEW.value
    elif any(re.search(p, lowered) for p in _UNVERIFIED_FACT_MARKERS):
        content_nature = ContentNature.FACTUAL_CLAIM_UNVERIFIED.value
        moderation_status = ModerationStatus.NEEDS_OFFICIAL_VERIFICATION.value
    else:
        is_guidance = any(
            kw in _normalize_search_text(raw_text)
            for kw in ("نصيحه", "نصائح", "توجيه", "انصح", "مراجعه", "tip", "advice", "guide")
        )
        content_nature = (
            ContentNature.STUDENT_EXPERIENCE_GUIDANCE.value
            if is_guidance
            else ContentNature.PERSONAL_OPINION.value
        )
        moderation_status = ModerationStatus.NEEDS_MODERATION_REVIEW.value

    # H6: Redact BOTH heading and body when sensitive/PII signals are detected.
    safe_topic = (
        "محتوى حساس محجوب (قيد المراجعة الإدارية)"
        if has_sensitive_signal
        else (first_line or "محتوى طلابي مجتمعي (قيد المراجعة)")
    )
    safe_summary = (
        "تم حجب النص الأصلي للمراجعة بسبب مؤشر حساسية أو بيانات شخصية."
        if has_sensitive_signal
        else cleaned
    )

    return CommunityRecord(
        community_id=community_id,
        dedup_group_id=dedup_group_id,
        duplicate_mentions_in_source=1,
        category=category,
        content_nature=content_nature,
        topic_ar=safe_topic,
        summary_sanitized_ar=safe_summary,
        identifiable_person_mentioned=identifiable_person,
        defamation_or_sensitivity_flag=has_sensitive_signal,
        moderation_status=moderation_status,
        original_source_link=None,
        publication_date=None,
        provenance_status=ProvenanceStatus.UNVERIFIED.value,
        privacy_review_status=ReviewStatus.PENDING.value,
        rights_review_status=ReviewStatus.PENDING.value,
        reputational_review_status=ReviewStatus.PENDING.value if identifiable_person else None,
        moderation_reviewer_id=None,
        moderation_reviewed_at=None,
        record_version=1,
        approval_version=0,
        approved_content_hash=None,
        approval_attestation=None,
        official_policy_separation_note=FIXED_SEPARATION_DISCLAIMER_AR,
        index_namespace=IndexNamespace.COMMUNITY_CANDIDATE_ONLY.value,
        isolated_from_official_kb=True,
        source_archive_path=source_archive_path,
        source_sha256=source_sha256,
        imported_to_vector_store=False,
        ingestion_job_id=ingestion_job_id,
        created_at=now_iso,
        updated_at=now_iso,
    )


def _quarantine_structured_item(
    raw_item: dict[str, Any],
    *,
    fallback_id: str,
    fallback_dedup: str,
    source_archive_path: str,
    source_sha256: str,
    ingestion_job_id: str,
    now_iso: str,
) -> CommunityRecord:
    """Normalize a JSON/JSONL community item while strictly enforcing quarantine & server bindings."""
    for key in ("topic_ar", "topic", "summary_sanitized_ar", "summary"):
        val = raw_item.get(key)
        if isinstance(val, str) and len(val) > MAX_FIELD_LENGTH:
            raise CommunityValidationError(
                f"Field '{key}' exceeds maximum allowed length ({MAX_FIELD_LENGTH})."
            )

    raw_cid = raw_item.get("community_id")
    community_id = str(raw_cid).strip() if isinstance(raw_cid, str) and raw_cid.strip() else fallback_id
    dedup_group_id = str(raw_item.get("dedup_group_id") or fallback_dedup).strip()

    topic_raw = str(raw_item.get("topic_ar") or raw_item.get("topic") or "محتوى طلابي مجتمعي")
    summary_raw = str(raw_item.get("summary_sanitized_ar") or raw_item.get("summary") or topic_raw)
    content_nature = str(raw_item.get("content_nature") or ContentNature.PERSONAL_OPINION.value)

    has_sensitive_signal, _ = detect_pii_or_defamation_signals(f"{topic_raw} {summary_raw}")
    sensitivity_flag = (raw_item.get("defamation_or_sensitivity_flag") is True) or has_sensitive_signal
    identifiable_person = (raw_item.get("identifiable_person_mentioned") is True) or any(
        re.search(p, f"{topic_raw} {summary_raw}".casefold()) for p in _PERSON_MENTION_MARKERS
    )

    # C1: Keep imported/untrusted claims strictly separate from trusted server attestations.
    imported_claims = {
        "claimed_source_link": raw_item.get("original_source_link"),
        "claimed_publication_date": raw_item.get("publication_date"),
        "claimed_provenance_status": raw_item.get("provenance_status"),
        "claimed_moderation_status": raw_item.get("moderation_status"),
    }

    if content_nature == ContentNature.UNOFFICIAL_CLAIM_ABOUT_OFFICIAL_REGULATION.value:
        moderation_status = ModerationStatus.EXCLUDED_MUST_USE_OFFICIAL_KB.value
    elif sensitivity_flag:
        moderation_status = ModerationStatus.BLOCKED_SENSITIVE_DEFAMATION_REVIEW.value
    elif content_nature == ContentNature.FACTUAL_CLAIM_UNVERIFIED.value:
        moderation_status = ModerationStatus.NEEDS_OFFICIAL_VERIFICATION.value
    else:
        moderation_status = ModerationStatus.NEEDS_MODERATION_REVIEW.value

    # H6: Redact sensitive heading/summary in stored record for blocked items.
    safe_topic = (
        "محتوى حساس محجوب (قيد المراجعة الإدارية)"
        if sensitivity_flag
        else sanitize_community_text(topic_raw, max_length=200)
    )
    safe_summary = (
        "تم حجب النص الأصلي للمراجعة بسبب مؤشر حساسية أو بيانات شخصية."
        if sensitivity_flag
        else sanitize_community_text(summary_raw, max_length=700)
    )

    # H4: Always bind server-computed source_sha256 and source_archive_path (never trust JSON values).
    return CommunityRecord(
        community_id=community_id,
        dedup_group_id=dedup_group_id,
        duplicate_mentions_in_source=max(1, int(raw_item.get("duplicate_mentions_in_source") or 1)),
        independent_author_key_hash=raw_item.get("independent_author_key_hash")
        if isinstance(raw_item.get("independent_author_key_hash"), str)
        else None,
        category=str(raw_item.get("category") or _infer_category(f"{topic_raw} {summary_raw}")),
        content_nature=content_nature,
        topic_ar=safe_topic,
        summary_sanitized_ar=safe_summary,
        identifiable_person_mentioned=identifiable_person,
        defamation_or_sensitivity_flag=sensitivity_flag,
        moderation_status=moderation_status,
        original_source_link=None,
        publication_date=None,
        provenance_status=ProvenanceStatus.UNVERIFIED.value,
        privacy_review_status=ReviewStatus.PENDING.value,
        rights_review_status=ReviewStatus.PENDING.value,
        reputational_review_status=ReviewStatus.PENDING.value if identifiable_person else None,
        moderation_reviewer_id=None,
        moderation_reviewed_at=None,
        record_version=1,
        approval_version=0,
        approved_content_hash=None,
        approval_attestation=None,
        imported_claims=imported_claims,
        official_policy_separation_note=FIXED_SEPARATION_DISCLAIMER_AR,
        index_namespace=IndexNamespace.COMMUNITY_CANDIDATE_ONLY.value,
        isolated_from_official_kb=True,
        source_archive_path=source_archive_path,
        source_sha256=source_sha256,
        imported_to_vector_store=False,
        ingestion_job_id=ingestion_job_id,
        created_at=now_iso,
        updated_at=now_iso,
    )


def parse_uploaded_community_content(
    filename: str,
    raw_text: str,
    *,
    source_archive_path: str,
    source_sha256: str,
    ingestion_job_id: str,
) -> list[CommunityRecord]:
    """Extract, validate bounds, and classify community records into QUARANTINE BEFORE any write."""
    now_iso = _utc_now_iso()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    short_hash = source_sha256[:8]
    records: list[CommunityRecord] = []

    if ext == "jsonl":
        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        if len(lines) > MAX_RECORDS_PER_UPLOAD:
            raise CommunityValidationError(
                f"Upload contains {len(lines)} records; maximum allowed is {MAX_RECORDS_PER_UPLOAD}."
            )
        for idx, line in enumerate(lines, start=1):
            item = json.loads(line)
            _check_json_depth(item)
            if not isinstance(item, dict):
                raise CommunityValidationError(f"Line {idx} must be a JSON object.")
            records.append(
                _quarantine_structured_item(
                    item,
                    fallback_id=f"CM-ING-{short_hash}-{idx:03d}",
                    fallback_dedup=f"DG-ING-{short_hash}-{idx:03d}",
                    source_archive_path=source_archive_path,
                    source_sha256=source_sha256,
                    ingestion_job_id=ingestion_job_id,
                    now_iso=now_iso,
                )
            )
        return records

    if ext == "json":
        parsed = json.loads(raw_text)
        _check_json_depth(parsed)
        items = parsed if isinstance(parsed, list) else [parsed]
        if len(items) > MAX_RECORDS_PER_UPLOAD:
            raise CommunityValidationError(
                f"Upload contains {len(items)} records; maximum allowed is {MAX_RECORDS_PER_UPLOAD}."
            )
        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise CommunityValidationError(f"Item {idx} must be a JSON object.")
            records.append(
                _quarantine_structured_item(
                    item,
                    fallback_id=f"CM-ING-{short_hash}-{idx:03d}",
                    fallback_dedup=f"DG-ING-{short_hash}-{idx:03d}",
                    source_archive_path=source_archive_path,
                    source_sha256=source_sha256,
                    ingestion_job_id=ingestion_job_id,
                    now_iso=now_iso,
                )
            )
        return records

    raw_blocks = [b.strip() for b in re.split(r"\n(?=#{1,3}\s)|\n{2,}", raw_text.strip()) if len(b.strip()) >= 10]
    if len(raw_blocks) > MAX_RECORDS_PER_UPLOAD:
        raise CommunityValidationError(
            f"Upload splits into {len(raw_blocks)} blocks; maximum allowed is {MAX_RECORDS_PER_UPLOAD}."
        )
    for idx, block in enumerate(raw_blocks, start=1):
        records.append(
            _classify_text_entry(
                block,
                community_id=f"CM-ING-{short_hash}-{idx:03d}",
                dedup_group_id=f"DG-ING-{short_hash}-{idx:03d}",
                source_archive_path=source_archive_path,
                source_sha256=source_sha256,
                ingestion_job_id=ingestion_job_id,
                now_iso=now_iso,
            )
        )
    return records


async def ingest_community_upload(
    *,
    filename: str,
    content_type: str,
    raw_bytes: bytes,
    uploaded_by: str,
) -> tuple[dict[str, Any], bool]:
    """Validate, atomically claim job by SHA-256, archive pointer, and quarantine records.

    Returns (job_dict, is_duplicate). Validates & parses BEFORE any durable write (H3/H4).
    """
    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    now_iso = _utc_now_iso()
    raw_text = raw_bytes.decode("utf-8")
    restricted_path = f"restricted_archive/COMMUNITY_CANDIDATE/{source_sha256[:12]}_{filename}"
    job_id = f"cmjob-{source_sha256[:16]}"

    # H3/H4: Parse & validate entire payload BEFORE any Firestore write!
    records = parse_uploaded_community_content(
        filename,
        raw_text,
        source_archive_path=restricted_path,
        source_sha256=source_sha256,
        ingestion_job_id=job_id,
    )

    # Check for duplicate community_ids within the same upload.
    seen_ids: set[str] = set()
    for rec in records:
        if rec.community_id in seen_ids:
            raise CommunityValidationError(f"Duplicate community_id '{rec.community_id}' inside upload.")
        seen_ids.add(rec.community_id)

    # H4: Atomically claim the full SHA-256 job BEFORE side effects.
    initial_job = CommunityIngestionJob(
        job_id=job_id,
        archive_id=source_sha256,
        source_sha256=source_sha256,
        filename=filename,
        status=IngestionJobStatus.CLAIMED.value,
        total_extracted=len(records),
        created_by=uploaded_by,
        created_at=now_iso,
        updated_at=now_iso,
    )
    claimed, existing_job = await claim_ingestion_job_atomic(initial_job.model_dump(mode="json"))
    if not claimed:
        return existing_job, True

    # H3/H6: Store only bounded archive metadata pointer in Firestore (never multi-MiB raw body).
    has_pii, _ = detect_pii_or_defamation_signals(raw_text[:512])
    preview = "[REDACTED_PREVIEW]" if has_pii else sanitize_community_text(raw_text[:256], max_length=256)
    archive = CommunityArchiveRecord(
        archive_id=source_sha256,
        filename=filename,
        content_type=content_type,
        byte_size=len(raw_bytes),
        source_sha256=source_sha256,
        restricted_storage_path=restricted_path,
        redacted_preview=preview,
        uploaded_by=uploaded_by,
        created_at=now_iso,
        isolated_from_official_kb=True,
    )
    await save_source_archive_idempotent(archive.model_dump(mode="json"))

    quarantined_count = 0
    blocked_sensitive_count = 0
    excluded_official_count = 0
    record_ids: list[str] = []

    try:
        for rec in records:
            rec_dict = rec.model_dump(mode="json")
            await create_community_record_strict(rec_dict)
            record_ids.append(rec.community_id)
            if rec.moderation_status == ModerationStatus.BLOCKED_SENSITIVE_DEFAMATION_REVIEW.value:
                blocked_sensitive_count += 1
            elif rec.moderation_status == ModerationStatus.EXCLUDED_MUST_USE_OFFICIAL_KB.value:
                excluded_official_count += 1
            else:
                quarantined_count += 1
    except Exception as exc:
        failed_job = initial_job.model_copy(
            update={
                "status": IngestionJobStatus.FAILED.value,
                "record_ids": record_ids,
                "quarantined_count": quarantined_count,
                "blocked_sensitive_count": blocked_sensitive_count,
                "excluded_official_count": excluded_official_count,
                "updated_at": _utc_now_iso(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        await update_ingestion_job(source_sha256, failed_job.model_dump(mode="json"))
        raise

    completed_job = initial_job.model_copy(
        update={
            "status": IngestionJobStatus.QUARANTINED.value,
            "total_extracted": len(records),
            "quarantined_count": quarantined_count,
            "blocked_sensitive_count": blocked_sensitive_count,
            "excluded_official_count": excluded_official_count,
            "published_count": 0,
            "record_ids": record_ids,
            "updated_at": _utc_now_iso(),
        }
    )
    saved_job = await update_ingestion_job(source_sha256, completed_job.model_dump(mode="json"))
    return saved_job, False


async def get_ingestion_job_status(job_id: str) -> dict[str, Any] | None:
    return await get_ingestion_job(job_id)


async def get_review_queue(
    *,
    moderation_status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    records = await list_community_records(moderation_status=moderation_status, limit=limit)
    config = get_config()
    enriched: list[dict[str, Any]] = []
    for rec in records:
        decision = evaluate_for_publication(
            rec,
            min_confidence=config.COMMUNITY_SOURCE_MIN_CONFIDENCE,
            require_published_namespace=True,
        )
        # H6: Redact sensitive previews in queue output.
        safe_rec = dict(rec)
        if safe_rec.get("defamation_or_sensitivity_flag") is True or safe_rec.get("moderation_status") == ModerationStatus.BLOCKED_SENSITIVE_DEFAMATION_REVIEW.value:
            safe_rec["topic_ar"] = "محتوى حساس محجوب (قيد المراجعة الإدارية)"
            safe_rec["summary_sanitized_ar"] = "تم حجب النص الأصلي للمراجعة الإدارية."
        enriched.append({
            **safe_rec,
            "publication_eligible": decision.allowed,
            "blocking_reasons": decision.reasons,
        })
    return enriched


async def apply_review_action(
    *,
    community_id: str,
    reviewer_uid: str,
    request: CommunityReviewActionRequest,
) -> dict[str, Any] | None:
    """Apply an authorized moderation action atomically with version-bound attestation (C1, H2, H4)."""
    existing = await get_community_record(community_id)
    if existing is None:
        return None

    current_version = int(existing.get("record_version") or 1)
    if request.expected_version is not None and request.expected_version != current_version:
        raise CommunityConcurrencyError(
            f"Expected record_version={request.expected_version}, actual={current_version}."
        )

    # H4: Monotonic retraction — regular approve cannot override a RETRACTED record.
    if (
        request.action == "approve"
        and existing.get("moderation_status") == ModerationStatus.RETRACTED.value
        and not request.reapprove_after_retraction
    ):
        raise CommunityMonotonicRetractionError(
            "Retracted records are monotonic and require explicit reapprove_after_retraction=true."
        )

    now_iso = _utc_now_iso()
    next_version = current_version + 1
    current_approval_version = int(existing.get("approval_version") or 0)

    merged = dict(existing)
    merged["record_version"] = next_version
    merged["moderation_reviewer_id"] = reviewer_uid
    merged["moderation_reviewed_at"] = now_iso
    merged["updated_at"] = now_iso
    merged["isolated_from_official_kb"] = True
    merged["official_policy_separation_note"] = FIXED_SEPARATION_DISCLAIMER_AR

    for field in (
        "original_source_link",
        "publication_date",
        "provenance_status",
        "privacy_review_status",
        "rights_review_status",
        "reputational_review_status",
        "defamation_or_sensitivity_flag",
        "identifiable_person_mentioned",
        "valid_until",
        "source_confidence",
    ):
        val = getattr(request, field)
        if val is not None:
            merged[field] = val

    retraction_marker: dict[str, Any] | None = None

    if request.action == "approve":
        # C1: Require independent captured publisher, valid capture timestamp, and SHA-256 evidence hash.
        pre_reasons: list[str] = []
        if not request.captured_publisher or not request.captured_publisher.strip():
            pre_reasons.append("MISSING_CAPTURED_PUBLISHER")
        captured_at = request.captured_at or now_iso
        if not is_valid_iso_timestamp(captured_at):
            pre_reasons.append("INVALID_CAPTURED_TIMESTAMP")
        if not is_valid_sha256_hex(request.evidence_hash):
            pre_reasons.append("MISSING_VALID_EVIDENCE_HASH")
        if pre_reasons:
            raise CommunityPublicationDeniedError(pre_reasons)

        next_approval_version = current_approval_version + 1
        merged["moderation_status"] = ModerationStatus.APPROVED_FOR_COMMUNITY_PUBLICATION.value
        merged["index_namespace"] = IndexNamespace.COMMUNITY_PUBLISHED.value
        merged["retracted_at"] = None
        merged["retraction_reason"] = None
        merged["approval_version"] = next_approval_version
        content_hash = compute_record_content_hash(merged)
        merged["approved_content_hash"] = content_hash

        person_mentioned = merged.get("identifiable_person_mentioned") is True
        rep_decision = (
            "PASS" if (person_mentioned and merged.get("reputational_review_status") == "PASS")
            else "FAIL" if person_mentioned
            else "NOT_APPLICABLE"
        )
        attestation = ApprovalAttestation(
            attestation_version=next_approval_version,
            approved_content_hash=content_hash,
            canonical_source_url=str(merged.get("original_source_link") or ""),
            captured_publisher=request.captured_publisher.strip(),
            captured_at=captured_at,
            publication_date=str(merged.get("publication_date") or ""),
            evidence_hash=str(request.evidence_hash).strip().lower(),
            reviewer_id=reviewer_uid,
            reviewed_at=now_iso,
            privacy_decision="PASS" if merged.get("privacy_review_status") == "PASS" else "FAIL",
            rights_decision="PASS" if merged.get("rights_review_status") == "PASS" else "FAIL",
            reputational_decision=rep_decision,
        )
        merged["approval_attestation"] = attestation.model_dump(mode="json")

        config = get_config()
        decision = evaluate_for_publication(
            merged,
            min_confidence=config.COMMUNITY_SOURCE_MIN_CONFIDENCE,
            require_published_namespace=True,
        )
        if not decision.allowed:
            raise CommunityPublicationDeniedError(decision.reasons)

    elif request.action == "reject":
        merged["moderation_status"] = ModerationStatus.REJECTED.value
        merged["index_namespace"] = IndexNamespace.COMMUNITY_CANDIDATE_ONLY.value
        merged["approval_attestation"] = None
        merged["approved_content_hash"] = None

    elif request.action == "retract":
        merged["moderation_status"] = ModerationStatus.RETRACTED.value
        merged["index_namespace"] = IndexNamespace.COMMUNITY_CANDIDATE_ONLY.value
        merged["retracted_at"] = now_iso
        merged["retraction_reason"] = request.retraction_reason or request.notes or "Retracted by moderator"
        retraction_marker = {
            "community_id": community_id,
            "revoked_approval_version": max(1, current_approval_version),
            "retracted_at": now_iso,
            "retracted_by": reviewer_uid,
            "reason": merged["retraction_reason"],
        }

    audit_entry = {
        "community_id": community_id,
        "action": request.action,
        "reviewer_uid": reviewer_uid,
        "timestamp": now_iso,
        "previous_version": current_version,
        "new_version": next_version,
        "approval_version": merged.get("approval_version"),
        "previous_status": existing.get("moderation_status"),
        "new_status": merged.get("moderation_status"),
        "notes": request.notes,
        "reapprove_after_retraction": request.reapprove_after_retraction,
        "retraction_reason": merged.get("retraction_reason"),
    }

    return await transition_community_record_atomic(
        community_id,
        expected_version=current_version,
        new_record=merged,
        audit_entry=audit_entry,
        retraction_marker=retraction_marker,
    )


async def retrieve_community_context(
    query: str,
    *,
    limit: int = 5,
) -> CommunityRetrievalResult:
    """Retrieve published, double-gated Community Knowledge matching `query` (H2, H5, M2).

    Enforces bounded timeout (preserving CancelledError), per-record schema safety,
    and meaningful topic/course/person token overlap (never category-only).
    """
    config = get_config()
    if not getattr(config, "COMMUNITY_RAG_ENABLED", False):
        return CommunityRetrievalResult(status="disabled", records=[])

    try:
        raw_candidates = await asyncio.wait_for(
            query_published_community_records(limit=100),
            timeout=RETRIEVAL_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Community Knowledge retrieval failed closed (%s)", type(exc).__name__)
        if getattr(config, "COMMUNITY_FAIL_CLOSED", True):
            return CommunityRetrievalResult(
                status="unavailable",
                records=[],
                failed_closed=True,
                error_reason=type(exc).__name__,
            )
        raise

    min_conf = float(getattr(config, "COMMUNITY_SOURCE_MIN_CONFIDENCE", 0.0))
    malformed_skipped = 0
    valid_candidates: list[dict[str, Any]] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            malformed_skipped += 1
            continue
        try:
            decision = evaluate_for_publication(
                item,
                min_confidence=min_conf,
                require_published_namespace=True,
            )
        except Exception:
            malformed_skipped += 1
            continue
        if decision.allowed:
            valid_candidates.append(item)

    approved = filter_retrieved_community(valid_candidates, min_confidence=min_conf)
    filtered_out = len(raw_candidates) - len(approved)

    if not approved:
        return CommunityRetrievalResult(
            status="empty",
            records=[],
            total_candidates_checked=len(raw_candidates),
            filtered_out_count=filtered_out,
            malformed_skipped_count=malformed_skipped,
        )

    # M2: Require genuine topic/course/person token overlap (overlap >= 1); never qualify on category alone.
    query_tokens = _tokenize_topic(query)
    if not query_tokens:
        return CommunityRetrievalResult(
            status="empty",
            records=[],
            total_candidates_checked=len(raw_candidates),
            filtered_out_count=filtered_out,
            malformed_skipped_count=malformed_skipped,
        )

    scored: list[tuple[int, str, dict[str, Any]]] = []
    for rec in approved:
        haystack = f"{rec.get('topic_ar', '')} {rec.get('summary_sanitized_ar', '')}"
        rec_tokens = _tokenize_topic(haystack)
        overlap = len(query_tokens & rec_tokens)
        if overlap >= 1:
            scored.append((overlap, str(rec.get("publication_date") or ""), rec))

    scored.sort(key=lambda triple: (triple[0], triple[1]), reverse=True)
    selected = [rec for _, _, rec in scored[:limit]]
    return CommunityRetrievalResult(
        status="ok" if selected else "empty",
        records=selected,
        total_candidates_checked=len(raw_candidates),
        filtered_out_count=filtered_out,
        malformed_skipped_count=malformed_skipped,
    )


async def verify_records_still_published(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Authoritative pre-emit check that no selected record was retracted mid-flight (H2)."""
    surviving: list[dict[str, Any]] = []
    for rec in records:
        cid = str(rec.get("community_id") or "")
        ver = int(rec.get("approval_version") or 0)
        if cid and ver >= 1 and await verify_record_not_revoked(cid, ver):
            surviving.append(rec)
    return surviving


def build_community_prompt_context(records: list[dict[str, Any]]) -> str:
    """Serialize double-gated community records into sanitized JSON without unverified counts (M2)."""
    safe_items = []
    for rec in records:
        safe_items.append({
            "community_id": sanitize_community_text(str(rec.get("community_id") or ""), max_length=64),
            "approval_version": int(rec.get("approval_version") or 0),
            "category": sanitize_community_text(str(rec.get("category") or ""), max_length=40),
            "content_nature": sanitize_community_text(str(rec.get("content_nature") or ""), max_length=64),
            "topic": sanitize_community_text(str(rec.get("topic_ar") or ""), max_length=200),
            "summary": sanitize_community_text(str(rec.get("summary_sanitized_ar") or ""), max_length=600),
            "original_source_link": str(rec.get("original_source_link") or ""),
            "publication_date": str(rec.get("publication_date") or ""),
            "disclaimer": FIXED_SEPARATION_DISCLAIMER_AR,
        })
    return json.dumps(
        {
            "availability": "available" if safe_items else "empty",
            "independence_note": "Independent student count is not established; do not infer or state a count of independent students.",
            "opinions": safe_items,
        },
        ensure_ascii=False,
    )


def render_community_section_markdown(
    records: list[dict[str, Any]],
    *,
    english: bool = False,
    unavailable: bool = False,
) -> str:
    """Programmatically render an attributed, dated Community section (H1, M2).

    Used on mixed queries so Official generation NEVER receives Community text,
    and citations strictly match rendered records only.
    """
    if english:
        header = "### Student Opinions & Experiences (Unofficial, Source-Dated)"
        if unavailable:
            return (
                f"{header}\n\n"
                "Community reviews are temporarily unavailable. Official Kuwait University "
                "information above is unaffected."
            )
        if not records:
            return (
                f"{header}\n\n"
                "There are currently no verified and approved student opinions available for this topic."
            )
        lines = [header, "", f"*{FIXED_SEPARATION_DISCLAIMER_EN}*", ""]
        for rec in records:
            topic = sanitize_community_text(str(rec.get("topic_ar") or "Student review"), max_length=160)
            summary = sanitize_community_text(str(rec.get("summary_sanitized_ar") or ""), max_length=400)
            url = str(rec.get("original_source_link") or "").strip()
            pub_date = str(rec.get("publication_date") or "").strip()
            lines.append(f"- **{topic}** ({pub_date}): {summary} — [Original post]({url})")
        return "\n".join(lines)

    header = "### تجارب وآراء الطلبة (غير رسمية وموثّقة المصدر)"
    if unavailable:
        return (
            f"{header}\n\n"
            "خدمة الآراء الطلابية غير متاحة مؤقتاً، بينما تبقى المعلومات الرسمية أعلاه منفصلة ومستقلة."
        )
    if not records:
        return (
            f"{header}\n\n"
            "لا تتوفر حالياً آراء أو تجارب طلابية موثّقة ومعتمدة حول هذا الموضوع."
        )
    lines = [header, "", f"*{FIXED_SEPARATION_DISCLAIMER_AR}*", ""]
    for rec in records:
        topic = sanitize_community_text(str(rec.get("topic_ar") or "رأي طلابي"), max_length=160)
        summary = sanitize_community_text(str(rec.get("summary_sanitized_ar") or ""), max_length=400)
        url = str(rec.get("original_source_link") or "").strip()
        pub_date = str(rec.get("publication_date") or "").strip()
        lines.append(f"- **{topic}** ({pub_date}): {summary} — [المصدر الأصلي]({url})")
    return "\n".join(lines)


def community_citations_for_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build genuine url_citation entries strictly from rendered verified community records."""
    citations: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for rec in records:
        url = str(rec.get("original_source_link") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        pub_date = str(rec.get("publication_date") or "").strip()
        topic = sanitize_community_text(str(rec.get("topic_ar") or "رأي طلابي"), max_length=120)
        title = f"{topic} (رأي طلابي — {pub_date})" if pub_date else f"{topic} (رأي طلابي)"
        citations.append({
            "type": "url_citation",
            "url": url,
            "title": title,
            "source_kind": "community_opinion",
            "community_id": str(rec.get("community_id") or ""),
            "approval_version": int(rec.get("approval_version") or 0),
            "start_index": 0,
            "end_index": 0,
        })
    return citations


def community_source_versions_for_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return canonical version refs attached to persisted/SSE community outputs (H2, H6)."""
    return [
        {
            "community_id": str(rec.get("community_id") or ""),
            "approval_version": int(rec.get("approval_version") or 0),
        }
        for rec in records
        if rec.get("community_id")
    ]


def community_abstention_answer(
    user_message: str,
    *,
    english: bool = False,
    unavailable: bool = False,
) -> str:
    """Deterministic honest abstention when zero verified community records exist or store is unavailable."""
    if english:
        if unavailable:
            return (
                "Community student reviews are temporarily unavailable. "
                "Kuniv AI fails closed and will not substitute unverified opinions or official regulations."
            )
        return (
            "There are currently no verified and approved student opinions or campus reviews "
            "available for this topic in Kuniv AI's Community Knowledge base.\n\n"
            "Community reviews are only shown when they have a verified public source link, "
            "publication date, and completed moderation review, and they never replace official "
            "Kuwait University announcements or regulations."
        )
    if unavailable:
        return (
            "خدمة المعرفة المجتمعية غير متاحة مؤقتاً. "
            "يتوقف Kuniv AI بشكل آمن ولا يعرض آراء غير موثّقة أو بدائل غير معتمدة."
        )
    return (
        "لا تتوفر حالياً آراء أو تجارب طلابية موثّقة ومعتمدة حول هذا الموضوع في قاعدة المعرفة المجتمعية.\n\n"
        "يعرض Kuniv AI آراء وتجارب الطلبة فقط بعد التحقق من رابط المنشور العام الأصلي وتاريخ نشره "
        "واجتياز المراجعة، كما أن هذه الآراء لا تُعد بديلاً عن اللوائح أو الإعلانات الرسمية لجامعة الكويت."
    )


def evaluate_community_dataset(
    records: list[dict[str, Any]],
    *,
    min_confidence: float = 0.0,
) -> dict[str, Any]:
    """Evaluate a dataset of community records against the fail-closed publication policy."""
    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}

    for rec in records:
        decision: Decision = evaluate_for_publication(
            rec,
            min_confidence=min_confidence,
            require_published_namespace=True,
        )
        entry = {
            "community_id": rec.get("community_id"),
            "dedup_group_id": rec.get("dedup_group_id"),
            "category": rec.get("category"),
            "content_nature": rec.get("content_nature"),
            "moderation_status": rec.get("moderation_status"),
            "provenance_status": rec.get("provenance_status"),
            "allowed": decision.allowed,
            "reasons": decision.reasons,
        }
        if decision.allowed:
            eligible.append(entry)
        else:
            blocked.append(entry)
            for reason in decision.reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

    return {
        "total_records": len(records),
        "eligible_count": len(eligible),
        "blocked_count": len(blocked),
        "reason_counts": dict(sorted(reason_counts.items())),
        "eligible": eligible,
        "blocked": blocked,
    }
