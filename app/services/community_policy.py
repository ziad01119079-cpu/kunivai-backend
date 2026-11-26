"""Fail-closed publication and post-retrieval gate for Community Knowledge.

Enforces the Kuniv AI Community Knowledge contract (C1, H2, H5, M2, COMM-004):
- Strict type validation (no string-valued booleans, no non-string enums, no crash on malformed URLs).
- Canonical public https:// post URLs only (rejects example/reserved domains, bare homepages, local/private hosts).
- Strict ISO publication date (YYYY-MM-DD) rejecting future dates.
- Strict ISO-8601 UTC timestamp validation for reviewer signoff and capture timestamps.
- Canonical v2 approval digest binding EVERY gate-driving metadata, provenance,
  compliance, isolation, reviewer, source/archive, and immutable evidence field.
- Independent `approval_attestation` with `digest_version == "v2"`, `evidence_id`,
  and `evidence_captured_by` distinct from `reviewer_id`.
- Re-scans topic, summary, and URL for PII and defamation signals on EVERY gate evaluation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from ipaddress import ip_address
from typing import Any, Iterable
from urllib.parse import urlparse

APPROVAL_DIGEST_VERSION = "v2"

ALLOWED_CONTENT_NATURES = frozenset({
    "PERSONAL_OPINION",
    "STUDENT_EXPERIENCE_GUIDANCE",
})
FORBIDDEN_CONTENT_NATURES = frozenset({
    "UNOFFICIAL_CLAIM_ABOUT_OFFICIAL_REGULATION",
    "FACTUAL_CLAIM_UNVERIFIED",
})
VALID_NAMESPACES = frozenset({
    "COMMUNITY_CANDIDATE_ONLY",
    "COMMUNITY_PUBLISHED",
})
RESERVED_TEST_DOMAINS = frozenset({
    "example.com",
    "example.org",
    "example.net",
    "localhost",
    "invalid",
    "test",
})

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# PII & defamation heuristics used during ingestion AND re-checked at publication/retrieval.
_KUWAIT_PHONE_RE = re.compile(r"(?<!\d)(?:\+?965[\s-]?)?[569]\d{7}(?!\d)")
_CIVIL_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SENSITIVE_DEFAMATION_PATTERNS = (
    r"(?:أسوأ|اسوأ|اسوء|أفشل|افشل|فاشل|سيء جداً|سيئ جدا|ظالم|نصاب|فاسد|حقير|متسلط)",
    r"(?:انحش|اهرب|لا تسجل عنده|لا تاخذ عنده|مستحيل تنجح عنده|يعقد الطلبة|يرسب الكل)",
    r"(?:worst professor|terrible professor|corrupt|unfair|avoid at all costs|impossible to pass)",
)

# Canonical v2 allowlist of EVERY gate-driving field bound into the approval digest.
# Deliberately excludes `approved_content_hash` and `approval_attestation` to avoid circularity.
APPROVAL_DIGEST_FIELDS: tuple[str, ...] = (
    "community_id",
    "dedup_group_id",
    "independent_author_key_hash",
    "category",
    "content_nature",
    "topic_ar",
    "summary_sanitized_ar",
    "original_source_link",
    "publication_date",
    "provenance_status",
    "source_confidence",
    "privacy_review_status",
    "rights_review_status",
    "reputational_review_status",
    "identifiable_person_mentioned",
    "defamation_or_sensitivity_flag",
    "valid_until",
    "index_namespace",
    "isolated_from_official_kb",
    "moderation_reviewer_id",
    "moderation_reviewed_at",
    "source_sha256",
    "source_archive_path",
    "approval_version",
    "evidence_id",
    "evidence_hash",
)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: list[str]


def compute_evidence_snapshot_sha256(
    *,
    canonical_source_url: str,
    captured_publisher: str,
    captured_at: str,
    publication_date: str,
    captured_snapshot_text: str,
) -> str:
    """Server-side canonical SHA-256 over an immutable captured evidence snapshot."""
    payload = {
        "canonical_source_url": canonical_source_url.strip(),
        "captured_publisher": captured_publisher.strip(),
        "captured_at": captured_at.strip(),
        "publication_date": publication_date.strip(),
        "captured_snapshot_text": captured_snapshot_text,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_record_content_hash(record: dict[str, Any]) -> str:
    """Canonical versioned (v2) approval digest binding EVERY gate-driving metadata & evidence field."""
    payload: dict[str, Any] = {"digest_version": APPROVAL_DIGEST_VERSION}
    for field in APPROVAL_DIGEST_FIELDS:
        val = record.get(field)
        # Preserve exact JSON types (bool vs str vs int vs float vs None) so type tampering alters the digest.
        if isinstance(val, (str, int, float, bool)) or val is None:
            payload[field] = val
        else:
            payload[field] = str(val)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_valid_public_url(url: Any) -> bool:
    """Allow only canonical public https:// post URLs (no example/local hosts, no bare homepages, no crash)."""
    if not isinstance(url, str) or not url.strip():
        return False
    u = url.strip()
    if len(u) > 2048 or any(ch.isspace() for ch in u):
        return False
    try:
        p = urlparse(u)
        _ = p.port
        host = (p.hostname or "").strip().lower()
    except ValueError:
        return False

    if p.scheme != "https" or not p.netloc or p.username or p.password:
        return False
    if not host or host in RESERVED_TEST_DOMAINS:
        return False
    if any(host.endswith(f".{reserved}") for reserved in RESERVED_TEST_DOMAINS) or host.endswith(".local") or host.endswith(".internal"):
        return False

    path = (p.path or "").strip()
    if not path or path == "/":
        return False

    try:
        ip = ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    except ValueError:
        if "." not in host:
            return False
    return True


def is_valid_iso_date(val: Any, *, today: date | None = None, allow_future: bool = False) -> bool:
    """Validate strict YYYY-MM-DD calendar date and reject future dates unless allowed."""
    if not isinstance(val, str) or not _ISO_DATE_RE.match(val.strip()):
        return False
    try:
        parsed = datetime.strptime(val.strip(), "%Y-%m-%d").date()
    except ValueError:
        return False
    if not allow_future:
        ref_today = today or datetime.now(timezone.utc).date()
        if parsed > ref_today:
            return False
    return True


def is_valid_iso_timestamp(val: Any) -> bool:
    """Validate strict ISO-8601 timestamp with timezone (e.g. 2026-11-25T16:00:00Z)."""
    if not isinstance(val, str) or not val.strip():
        return False
    raw = val.strip()
    if "T" not in raw:
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt.tzinfo is not None


def is_valid_sha256_hex(val: Any) -> bool:
    return isinstance(val, str) and bool(_SHA256_HEX_RE.match(val.strip().lower()))


def is_expired(valid_until: Any, *, today: date | None = None) -> bool:
    """Return True if valid_until is present and strictly before today (or malformed)."""
    if valid_until is None or valid_until == "":
        return False
    if not is_valid_iso_date(valid_until, allow_future=True):
        return True
    cutoff = datetime.strptime(str(valid_until).strip(), "%Y-%m-%d").date()
    ref_today = today or datetime.now(timezone.utc).date()
    return cutoff < ref_today


def detect_pii_or_defamation_signals(text: Any) -> tuple[bool, list[str]]:
    """Detect obvious PII or harsh defamation/attack phrases."""
    if not isinstance(text, str) or not text:
        return False, []
    signals: list[str] = []
    if _KUWAIT_PHONE_RE.search(text):
        signals.append("PII_PHONE_NUMBER")
    if _CIVIL_ID_RE.search(text):
        signals.append("PII_CIVIL_ID")
    if _EMAIL_RE.search(text):
        signals.append("PII_EMAIL")
    lowered = text.casefold()
    for pattern in _SENSITIVE_DEFAMATION_PATTERNS:
        if re.search(pattern, lowered):
            signals.append("SENSITIVE_DEFAMATION_SIGNAL")
            break
    return (len(signals) > 0), signals


def evaluate_for_publication(
    record: Any,
    *,
    min_confidence: float = 0.0,
    today: date | None = None,
    require_published_namespace: bool = False,
) -> Decision:
    """Evaluate a single community record against all fail-closed publication gates."""
    if not isinstance(record, dict):
        return Decision(allowed=False, reasons=["INVALID_RECORD_TYPE"])

    reasons: list[str] = []

    mod_status = record.get("moderation_status")
    if not isinstance(mod_status, str):
        reasons.append("INVALID_MODERATION_STATUS_TYPE")
    elif mod_status == "RETRACTED" or record.get("retracted_at"):
        reasons.append("RETRACTED_RECORD")

    nature = record.get("content_nature")
    if not isinstance(nature, str):
        reasons.append("INVALID_CONTENT_NATURE_TYPE")
    elif nature in FORBIDDEN_CONTENT_NATURES:
        reasons.append(f"FORBIDDEN_CONTENT_NATURE:{nature}")
    elif nature not in ALLOWED_CONTENT_NATURES:
        reasons.append(f"UNKNOWN_CONTENT_NATURE:{nature}")

    if mod_status != "APPROVED_FOR_COMMUNITY_PUBLICATION":
        reasons.append(f"MODERATION_NOT_APPROVED:{mod_status}")

    # Strict boolean checks: string-valued "false"/"true" must fail closed (C1).
    sens_flag = record.get("defamation_or_sensitivity_flag")
    if type(sens_flag) is not bool:
        reasons.append("INVALID_SENSITIVITY_FLAG_TYPE")
    elif sens_flag is not False:
        reasons.append("SENSITIVITY_OR_DEFAMATION_BLOCKED")

    person_flag = record.get("identifiable_person_mentioned")
    if type(person_flag) is not bool:
        reasons.append("INVALID_PERSON_FLAG_TYPE")
    elif person_flag is True:
        if record.get("reputational_review_status") != "PASS":
            reasons.append("PERSON_REPUTATION_REVIEW_MISSING")

    if record.get("provenance_status") != "SOURCE_VERIFIED":
        reasons.append(f"PROVENANCE_UNVERIFIED:{record.get('provenance_status')}")

    url = record.get("original_source_link")
    if not is_valid_public_url(url):
        reasons.append("NO_VALID_PUBLIC_POST_URL")

    pub_date = record.get("publication_date")
    if not is_valid_iso_date(pub_date, today=today, allow_future=False):
        if is_valid_iso_date(pub_date, today=today, allow_future=True):
            reasons.append("FUTURE_PUBLICATION_DATE")
        else:
            reasons.append("NO_VALID_PUBLICATION_DATE")

    if record.get("privacy_review_status") != "PASS":
        reasons.append("PRIVACY_REVIEW_NOT_PASSED")

    if record.get("rights_review_status") != "PASS":
        reasons.append("RIGHTS_REVIEW_NOT_PASSED")

    reviewer_id = record.get("moderation_reviewer_id")
    reviewed_at = record.get("moderation_reviewed_at")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip() or not is_valid_iso_timestamp(reviewed_at):
        reasons.append("MISSING_ACCOUNTABLE_SIGNOFF")

    isolated = record.get("isolated_from_official_kb")
    if type(isolated) is not bool or isolated is not True:
        reasons.append("NOT_ISOLATED_FROM_OFFICIAL_KB")

    namespace = record.get("index_namespace")
    if require_published_namespace:
        if namespace != "COMMUNITY_PUBLISHED":
            reasons.append(f"NAMESPACE_NOT_PUBLISHED:{namespace}")
    elif not isinstance(namespace, str) or namespace not in VALID_NAMESPACES:
        reasons.append(f"INVALID_NAMESPACE:{namespace}")

    if is_expired(record.get("valid_until"), today=today):
        reasons.append("EXPIRED_RECORD")

    # Re-scan topic, summary, and URL for PII / defamation signals even if flags claim clean (C1).
    combined_text = " ".join(
        str(record.get(k) or "") for k in ("topic_ar", "summary_sanitized_ar", "original_source_link")
    )
    has_pii_or_defam, signals = detect_pii_or_defamation_signals(combined_text)
    if has_pii_or_defam:
        reasons.append("CONTENT_PII_OR_SENSITIVE_DETECTED:" + ",".join(signals))

    # Version-bound independent approval attestation & full v2 digest check (C1 / COMM-004).
    attestation = record.get("approval_attestation")
    if not isinstance(attestation, dict):
        reasons.append("MISSING_APPROVAL_ATTESTATION")
    else:
        if attestation.get("digest_version") != APPROVAL_DIGEST_VERSION:
            reasons.append("UNSUPPORTED_ATTESTATION_DIGEST_VERSION")

        expected_hash = compute_record_content_hash(record)
        att_version = attestation.get("attestation_version")
        rec_approval_version = record.get("approval_version")
        if (
            type(att_version) is not int
            or att_version < 1
            or type(rec_approval_version) is not int
            or att_version != rec_approval_version
        ):
            reasons.append("APPROVAL_VERSION_MISMATCH")
        if attestation.get("approved_content_hash") != expected_hash or record.get("approved_content_hash") != expected_hash:
            reasons.append("CONTENT_HASH_MISMATCH_AFTER_APPROVAL")
        if attestation.get("canonical_source_url") != url:
            reasons.append("ATTESTATION_URL_MISMATCH")
        if attestation.get("publication_date") != pub_date:
            reasons.append("ATTESTATION_DATE_MISMATCH")
        if not isinstance(attestation.get("captured_publisher"), str) or not attestation["captured_publisher"].strip():
            reasons.append("MISSING_CAPTURED_PUBLISHER")
        if not is_valid_iso_timestamp(attestation.get("captured_at")):
            reasons.append("INVALID_CAPTURED_TIMESTAMP")

        att_ev_id = attestation.get("evidence_id")
        if not isinstance(att_ev_id, str) or not att_ev_id.strip() or att_ev_id != record.get("evidence_id"):
            reasons.append("MISSING_OR_MISMATCHED_EVIDENCE_ID")

        att_ev_hash = attestation.get("evidence_hash")
        if not is_valid_sha256_hex(att_ev_hash) or att_ev_hash != record.get("evidence_hash"):
            reasons.append("MISSING_VALID_EVIDENCE_HASH")

        captured_by = attestation.get("evidence_captured_by")
        if not isinstance(captured_by, str) or not captured_by.strip():
            reasons.append("MISSING_EVIDENCE_VERIFIER")
        elif isinstance(reviewer_id, str) and captured_by.strip() == reviewer_id.strip():
            reasons.append("SELF_ATTESTED_EVIDENCE_FORBIDDEN")

        if attestation.get("reviewer_id") != reviewer_id or not is_valid_iso_timestamp(attestation.get("reviewed_at")):
            reasons.append("ATTESTATION_SIGNOFF_MISMATCH")
        if attestation.get("privacy_decision") != "PASS" or attestation.get("rights_decision") != "PASS":
            reasons.append("ATTESTATION_COMPLIANCE_NOT_PASSED")
        if person_flag is True and attestation.get("reputational_decision") != "PASS":
            reasons.append("ATTESTATION_REPUTATION_NOT_PASSED")

    if min_confidence > 0.0:
        conf = record.get("source_confidence")
        if not isinstance(conf, (int, float)) or isinstance(conf, bool) or float(conf) < min_confidence:
            reasons.append("BELOW_MIN_SOURCE_CONFIDENCE")

    return Decision(allowed=(len(reasons) == 0), reasons=reasons)


def filter_retrieved_community(
    records: Iterable[Any],
    *,
    min_confidence: float = 0.0,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Second-gate post-retrieval filter + canonical URL/dedup filtering (C1, H5, M2)."""
    approved: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_dedup_groups: set[str] = set()
    seen_author_topic_keys: set[tuple[str, str]] = set()

    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            decision = evaluate_for_publication(
                record,
                min_confidence=min_confidence,
                today=today,
                require_published_namespace=True,
            )
        except Exception:
            continue
        if not decision.allowed:
            continue

        canonical_url = str(record.get("original_source_link") or "").strip().casefold()
        if canonical_url:
            if canonical_url in seen_urls:
                continue
            seen_urls.add(canonical_url)

        dedup_group = str(record.get("dedup_group_id") or "").strip()
        if dedup_group:
            if dedup_group in seen_dedup_groups:
                continue
            seen_dedup_groups.add(dedup_group)

        author_hash = str(record.get("independent_author_key_hash") or "").strip()
        topic_key = str(record.get("topic_ar") or "").strip().casefold()
        if author_hash and topic_key:
            pair = (author_hash, topic_key)
            if pair in seen_author_topic_keys:
                continue
            seen_author_topic_keys.add(pair)

        approved.append(record)

    return approved


def sanitize_community_text(text: Any, *, max_length: int = 800) -> str:
    """Neutralize prompt-injection markers and control characters in untrusted text."""
    if not isinstance(text, str):
        return ""
    cleaned = _CONTROL_CHARS_RE.sub(" ", text)
    cleaned = cleaned.replace("<", "&lt;").replace(">", "&gt;")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_length:
        cleaned = cleaned[: max_length - 1].rstrip() + "…"
    return cleaned
