# Codex Senior Code Review Handoff — Community Knowledge (`COMM-006`)

## 1. Git Baseline & Scope
- **Source Repository:** `ziad01119079-cpu/kunivai-backend` (Draft PR #1)
- **Base Branch / SHA:** `codex/rag-quality-sprint-1` @ `7836066618f2155584425e4569bfc61d885ebd48`
- **Feature Branch / Worktree:** `feat/community-knowledge` (`F:/kuwaitchatai-backend/.community-wt`)
- **Feature Flags (all OFF / fail-closed by default):** `COMMUNITY_INGESTION_ENABLED=false`, `COMMUNITY_RAG_ENABLED=false`, `COMMUNITY_FAIL_CLOSED=true`, `COMMUNITY_SOURCE_MIN_CONFIDENCE=0.0`
- **Status:** **Offline/Staging/Production NO-GO** until independent Codex review and live staging gates pass.

## 2. Safety Boundaries Implemented (`COMM-006`)
- **C1 (Evidence Trust Model):** Dedicated `verify_community_evidence_verifier` custom-claim dependency on `POST /admin/community/evidence` (separate from generic `verify_admin`), uploader excluded from verifier & approver roles, canonical text fingerprint (`compute_canonical_text_fingerprint`) rejecting reused upload content, claim-span applicability (`evidence_supports_candidate_claim`), and minimized API response (omitting raw snapshot text). *Unimplemented live gate:* automated external original-source capture/verification is intentionally outside offline scope; unverified real records remain quarantined.
- **H1 (Monotonic Ingestion Fence):** Transactional monotonic `attempt_fence` checked on every record write (`create_community_record_fenced`) and job transition (`update_ingestion_job_fenced`); stale owners cannot overwrite newer leases or terminal `QUARANTINED` state; `duplicate_completed` verifies current manifest and durable record existence.
- **H2/H3 (Sequenced Local & Redis Replay):** `RequestCoalescer` redesigned with single sequenced event log + `asyncio.Condition` + per-subscriber cursor + bounded lag + per-delivery revalidation; `RedisCoordinator` persists `has_official_chunk` for mid-stream resume, emits one atomic redacted terminal event (unique SSE ID), and rejects malformed/bool version types cleanly.
- **H4 (Partial Revocation Integrity):** Pure Community generation never relabels multi-source LLM output after partial revocation — discards LLM text and renders fresh deterministic excerpts from verified surviving records only.
- **H5 (v3 Canonical Digest & Authoritative Delivery Check):** `v3` approval digest binds all record AND attestation-only fields (`captured_publisher`, `captured_at`, `evidence_captured_by`, `evidence_content_fingerprint`, exact `reviewed_at == moderation_reviewed_at`); authoritative `verify_record_not_revoked` re-checks full `evaluate_for_publication` (digest + expiry) and linked evidence artifact existence.
- **H6 (Strict Official Origin & Feature-OFF Revocation):** Strict `ku.edu.kw` hostname check (`_is_strict_official_ku_url`); revoked Community history stays blocked even when `COMMUNITY_RAG_ENABLED=false`; server-persisted `serverProvenanceKind` preserves ordinary study continuity when enabled.
- **M1–M3:** Emergency retraction exempt from 30/min write limiter + bounded UID map; Markdown metacharacters escaped in rendered Community text (`escape_markdown_text`); restricted archive retention & IAM plan documented (not deployed).

## 3. Acceptance Traceability Matrix (`T01`–`T24`)
| Cases | Automated Test Coverage |
| --- | --- |
| `T01`–`T04`, `T11`, `T13` | `tests/test_community_routing.py::test_official_acceptance_cases_never_route_to_community_t01_to_t04_t11_t13` |
| `T05`, `T06`, `T08`, `T12`, `T15`, `T16` | `tests/test_community_routing.py::test_community_opinion_acceptance_cases_route_to_community_when_enabled`, `tests/test_community_policy.py::test_retrieval_second_gate_discards_blocked_malformed_and_deduplicates_t16_t20` |
| `T07`, `T10`, `T21` | `tests/test_community_routing.py::test_mixed_official_and_community_acceptance_cases_t07_t10_t21`, `tests/test_community_chat_sse.py::test_h1_mixed_query_never_sends_community_text_to_official_generator_and_composes_separately` |
| `T09` | `tests/test_community_routing.py::test_live_operational_hours_route_to_time_sensitive_when_enabled_t09` |
| `T14`, `T22` | `tests/test_community_routing.py::test_private_schedule_and_private_archive_never_route_to_community_t14_t22`, `tests/test_community_chat_sse.py::test_h6_t22_pure_community_excludes_private_history_schedules_and_study_context` |
| `T17` | `tests/test_community_policy.py::test_prompt_injection_sanitization_and_pii_detection_t17` |
| `T18`, `T19` | `tests/test_community_admin.py` (quarantine-only upload, resumable ingestion, monotonic retraction), `tests/test_community_repository_transaction.py` |
| `T20`, `T23`, `T24` | `tests/test_community_policy.py` (second gate discard, missing date, named-person reputational review) |
| `C1`, `H1`–`H6` | `tests/test_comm006_regressions.py` (TDD reproductions converted to passing regressions) |

## 4. Oracle Cloud Ampere A1 Rollback & Replay Containment
Keep `COMMUNITY_RAG_ENABLED=false` and `COMMUNITY_INGESTION_ENABLED=false` in the private server-side container env; drain/stop the candidate container behind Caddy (preserving container, Redis AOF, and logs); restore the pinned previous ARM64 image on loopback `:8080`; recheck `/health`, `/ready`, authenticated SSE canary, and replay. Note that disabling new Community retrieval still enforces revocation checks on prior Community history, while already-delivered client SSE text cannot be recalled.

## 5. Remaining Unverified Staging / Production Gates
1. Live Firestore client-denial rules (`community_*` `allow read, write: if false`) and least-privilege Admin SDK IAM.
2. Real Firebase emulator transaction retry/contention tests (unit fakes do not prove emulator retry semantics).
3. Two-replica Oracle Cloud Ampere A1 / Caddy / Redis retract-during-stream and reconnect replay rehearsal.
4. Authorized restricted original-file object storage and real original-source capture pipeline before any live Community publication (`0 / 18` legacy records eligible).
