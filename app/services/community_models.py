"""Pydantic models and domain enums for Community Knowledge.

Community Knowledge is strictly isolated from Official University Knowledge
and student private schedules. Every ingested item starts in quarantine,
separates unverified imported claims from server-managed approval attestations,
and links every approval to an immutable server-verified evidence artifact.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ContentNature(str, Enum):
    PERSONAL_OPINION = "PERSONAL_OPINION"
    STUDENT_EXPERIENCE_GUIDANCE = "STUDENT_EXPERIENCE_GUIDANCE"
    UNOFFICIAL_CLAIM_ABOUT_OFFICIAL_REGULATION = "UNOFFICIAL_CLAIM_ABOUT_OFFICIAL_REGULATION"
    FACTUAL_CLAIM_UNVERIFIED = "FACTUAL_CLAIM_UNVERIFIED"


class ModerationStatus(str, Enum):
    QUARANTINED = "QUARANTINED"
    NEEDS_MODERATION_REVIEW = "NEEDS_MODERATION_REVIEW"
    NEEDS_OFFICIAL_VERIFICATION = "NEEDS_OFFICIAL_VERIFICATION"
    BLOCKED_SENSITIVE_DEFAMATION_REVIEW = "BLOCKED_SENSITIVE_DEFAMATION_REVIEW"
    EXCLUDED_MUST_USE_OFFICIAL_KB = "EXCLUDED_MUST_USE_OFFICIAL_KB"
    APPROVED_FOR_COMMUNITY_PUBLICATION = "APPROVED_FOR_COMMUNITY_PUBLICATION"
    REJECTED = "REJECTED"
    RETRACTED = "RETRACTED"


class ProvenanceStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    SOURCE_VERIFIED = "SOURCE_VERIFIED"
    DISPUTED = "DISPUTED"


class ReviewStatus(str, Enum):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"


class CommunityCategory(str, Enum):
    PROFESSORS = "professors"
    COURSES = "courses"
    CAMPUS_SERVICES = "campus_services"
    STUDYING = "studying"
    CAMPUS_DINING = "campus_dining"
    CAMPUS_LIFE = "campus_life"
    OTHER = "other"


class IndexNamespace(str, Enum):
    COMMUNITY_CANDIDATE_ONLY = "COMMUNITY_CANDIDATE_ONLY"
    COMMUNITY_PUBLISHED = "COMMUNITY_PUBLISHED"


class IngestionJobStatus(str, Enum):
    CLAIMED = "CLAIMED"
    UPLOADED = "UPLOADED"
    EXTRACTED = "EXTRACTED"
    CLASSIFIED = "CLASSIFIED"
    QUARANTINED = "QUARANTINED"
    FAILED = "FAILED"


class EvidenceArtifactCreateRequest(BaseModel):
    """Request to register an immutable independently captured source evidence artifact."""

    canonical_source_url: str
    captured_publisher: str
    captured_at: str
    publication_date: str
    captured_snapshot_text: str = Field(..., min_length=10, max_length=16000)


class EvidenceArtifact(BaseModel):
    """Immutable server-verified evidence artifact stored in `community_evidence_artifacts`."""

    evidence_id: str
    canonical_source_url: str
    captured_publisher: str
    captured_at: str
    publication_date: str
    captured_snapshot_text: str
    content_fingerprint: str
    computed_sha256: str
    captured_by: str
    created_at: str
    immutable: bool = True


class ApprovalAttestation(BaseModel):
    """Server-managed, version-bound approval record tied to full v3 approval digest and evidence artifact."""

    digest_version: Literal["v3"] = "v3"
    attestation_version: int = Field(ge=1)
    approved_content_hash: str
    evidence_id: str
    evidence_hash: str
    evidence_content_fingerprint: str | None = None
    evidence_captured_by: str
    canonical_source_url: str
    captured_publisher: str
    captured_at: str
    publication_date: str
    reviewer_id: str
    reviewed_at: str
    privacy_decision: Literal["PASS", "FAIL"]
    rights_decision: Literal["PASS", "FAIL"]
    reputational_decision: Literal["PASS", "FAIL", "NOT_APPLICABLE"] = "NOT_APPLICABLE"


class CommunityRecord(BaseModel):
    community_id: str
    dedup_group_id: str
    duplicate_mentions_in_source: int = Field(default=1, ge=1)
    independent_author_key_hash: str | None = None
    category: str = CommunityCategory.OTHER.value
    content_nature: str = ContentNature.PERSONAL_OPINION.value
    topic_ar: str
    summary_sanitized_ar: str
    identifiable_person_mentioned: bool = False
    defamation_or_sensitivity_flag: bool = False
    moderation_status: str = ModerationStatus.NEEDS_MODERATION_REVIEW.value
    original_source_link: str | None = None
    publication_date: str | None = None
    provenance_status: str = ProvenanceStatus.UNVERIFIED.value
    privacy_review_status: str = ReviewStatus.PENDING.value
    rights_review_status: str = ReviewStatus.PENDING.value
    reputational_review_status: str | None = None
    moderation_reviewer_id: str | None = None
    moderation_reviewed_at: str | None = None
    # Version-bound approval attestation & full canonical digest (C1 / H2 / H4 / COMM-004)
    record_version: int = Field(default=1, ge=1)
    approval_version: int = Field(default=0, ge=0)
    evidence_id: str | None = None
    evidence_hash: str | None = None
    approved_content_hash: str | None = None
    approval_attestation: ApprovalAttestation | None = None
    # Imported claims kept strictly separate from trusted server attestations
    imported_claims: dict | None = None
    official_policy_separation_note: str = (
        "رأي وتجربة طلابية غير رسمية؛ لا تمثل لائحة أو قراراً رسمياً من جامعة الكويت."
    )
    index_namespace: str = IndexNamespace.COMMUNITY_CANDIDATE_ONLY.value
    isolated_from_official_kb: bool = True
    source_archive_path: str | None = None
    source_vs_file_id: str | None = None
    source_sha256: str | None = None
    uploaded_by: str | None = None
    canonical_text_fingerprint: str | None = None
    imported_to_vector_store: bool = False
    valid_until: str | None = None
    source_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    ingestion_job_id: str | None = None
    retracted_at: str | None = None
    retraction_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CommunityArchiveRecord(BaseModel):
    """Bounded Firestore metadata pointer for a restricted raw source archive (H3)."""

    archive_id: str
    filename: str
    content_type: str
    byte_size: int = Field(ge=1)
    source_sha256: str
    restricted_storage_path: str
    redacted_preview: str = ""
    uploaded_by: str
    created_at: str
    isolated_from_official_kb: bool = True


class CommunityIngestionJob(BaseModel):
    job_id: str
    archive_id: str
    source_sha256: str
    filename: str
    status: str = IngestionJobStatus.CLAIMED.value
    attempt_fence: int = Field(default=1, ge=1)
    attempt_id: str | None = None
    lease_expires_at: float | None = None
    expected_record_ids: list[str] = Field(default_factory=list)
    total_extracted: int = 0
    quarantined_count: int = 0
    blocked_sensitive_count: int = 0
    excluded_official_count: int = 0
    published_count: int = 0
    record_ids: list[str] = Field(default_factory=list)
    created_by: str
    created_at: str
    updated_at: str
    error: str | None = None


class CommunityReviewActionRequest(BaseModel):
    action: Literal["approve", "reject", "retract"]
    expected_version: int | None = Field(default=None, ge=1)
    evidence_id: str | None = None
    original_source_link: str | None = None
    publication_date: str | None = None
    captured_publisher: str | None = None
    captured_at: str | None = None
    evidence_hash: str | None = None
    provenance_status: Literal["UNVERIFIED", "SOURCE_VERIFIED", "DISPUTED"] | None = None
    privacy_review_status: Literal["PENDING", "PASS", "FAIL"] | None = None
    rights_review_status: Literal["PENDING", "PASS", "FAIL"] | None = None
    reputational_review_status: Literal["PENDING", "PASS", "FAIL"] | None = None
    defamation_or_sensitivity_flag: bool | None = None
    identifiable_person_mentioned: bool | None = None
    valid_until: str | None = None
    source_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reapprove_after_retraction: bool = False
    notes: str | None = None
    retraction_reason: str | None = None


class CommunityRetrievalResult(BaseModel):
    status: Literal["ok", "empty", "unavailable", "disabled"] = "empty"
    records: list[dict] = Field(default_factory=list)
    total_candidates_checked: int = 0
    filtered_out_count: int = 0
    malformed_skipped_count: int = 0
    failed_closed: bool = False
    error_reason: str | None = None
