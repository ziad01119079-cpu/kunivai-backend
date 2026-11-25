# Codex Senior Code Review Handoff — Community Knowledge (`COMM-002`)

## 1. Git Baseline & Scope
- **Source Repository:** `kuwaitchatai-backend`
- **Base Branch / Commit:** `codex/rag-quality-sprint-1` @ `7836066618f2155584425e4569bfc61d885ebd48`
- **Feature Branch / Worktree:** `feat/community-knowledge` (`F:/kuwaitchatai-backend/.community-wt`)
- **Workflow Issue:** `EslamDiab123/kuniv-ai-workflow#2` (`COMM-002`)
- **Status:** Offline remediation implemented and tested; **Staging NO-GO / Production NO-GO** pending Codex re-review and live staging validation. No merge, no deploy, no paid AI calls.

## 2. Codex Findings Remediated (C1, H1–H6, M1–M4, L1–L2)
- **C1 (Provenance & Version-Bound Attestation):** Strict bool/enum/URL/date/timestamp validation (rejects `example.*`, bare homepages, future dates, non-timestamp signoffs, string booleans, post-approval tampering, body PII); server-managed `approval_attestation` with `approved_content_hash`, `captured_publisher`, `captured_at`, `evidence_hash`; post-retrieval requires `COMMUNITY_PUBLISHED`.
- **H1 (Official/Community Separation):** Official generator never receives Community text. Mixed queries run Official independently and programmatically append a dated, attributed Community section with citations strictly matching rendered records; either half survives if the other lacks evidence.
- **H2 (Retraction Across Replicas/SSE/History):** Authoritative `verify_record_not_revoked()` on cache hits and pre-emit; Redis revocation hash broadcast when `COORDINATION_BACKEND=redis`; `community_source_versions` attached to SSE `done` and citations; retracted prior messages omitted from subsequent chat history.
- **H3 (Firestore 1 MiB & Resource Safety):** Upload cap lowered to 256 KiB (`413`), max 100 records, max JSON depth 6, max field length 2000 validated before durable writes; Firestore archive stores bounded metadata pointer only.
- **H4 (Concurrency & State Integrity):** Atomic SHA-256 job claim via `.create()` before side effects; create-only records (`409` on ID collision); server-derived `source_sha256`/`source_archive_path`; version-checked atomic batch for record + audit + revocation; monotonic retraction (`409`).
- **H5 (Malformed-Record Fail-Closed SSE):** Safe URL/type checks (no `ValueError`/`TypeError` on malformed IPv6 or list types); per-record error isolation with 3s retrieval timeout inside SSE `try` boundary; empty-completed provider response falls back to programmatic rendering instead of silent empty `done`.
- **H6 (Private Context & Storage Isolation):** Pure Community generation passes only `[current_user_message]` + `get_community_only_prompt(...)` (zero private history, schedules, or study context); sensitive headings redacted on ingest and queue previews; Firestore client-denial rules documented.
- **M1–M4:** Community routing gated behind `community_enabled=config.COMMUNITY_RAG_ENABLED` (exact baseline parity when OFF); retrieval requires topic token overlap `>= 1` (never category-only) and omits unverified student counts; UID-scoped admin write rate limit + independent retraction when `COMMUNITY_INGESTION_ENABLED=false`; expanded negative regression tests.
- **L1–L2:** Updated Oracle Cloud Ampere A1 Docker/Caddy/Redis rollback runbook; removed unused `COMMUNITY_VECTOR_STORE_ID`.

## 3. Publication Eligibility (Existing 18 Legacy Records)
- **0 / 18 eligible, 18 / 18 blocked** (see `docs/community_publication_eligibility_report.md`). All 18 remain quarantined.

## 4. Remaining Staging Blockers (Unverified Live)
1. Live Firebase staging verification of Firestore client-denial rules and least-privilege Admin SDK IAM.
2. Two-replica Oracle staging rehearsal through Caddy + Redis for retract-during-stream and reconnect replay.
3. Sourcing verified public `https://` URLs, ISO dates, and completed human reviews before any live Community record can be published.
