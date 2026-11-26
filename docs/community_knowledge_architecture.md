# Kuniv AI — Community Knowledge Architecture & Operations Guide

## 1. Overview & Isolation Boundaries

Community Knowledge enables Kuniv AI to ingest, quarantine, moderate, and optionally retrieve **verified, dated, subjective student opinions and experiences** while maintaining **strict physical and logical isolation** from:

1. **Official University Knowledge** (`OPENAI_VECTOR_STORE_ID` + official web search) — Official generators never receive Community text. Mixed queries compose Official and Community sections independently on the server.
2. **Private Student Data** (`users/{uid}/schedules`, `users/{uid}/chats`, uploaded study context) — Pure Community generation never receives private conversation history, student schedules, or uploaded study notes.

## 2. Storage, Firestore Rules & IAM

Dedicated Firestore collections (isolated from `users/{uid}/*` and Official Vector Store):

- `community_source_archives/{source_sha256}` — bounded metadata pointer (`≤ 4 KiB`, redacted preview only; never stores multi-MiB raw bodies).
- `community_ingestion_jobs/{source_sha256}` — atomically claimed via `.create()` before side effects.
- `community_records/{community_id}` — create-only on ingestion; version-checked (`record_version`) transactional transitions on moderation.
- `community_audit_logs/{auto_id}` — written atomically in the same batch as every moderation/retraction transition.
- `community_revocations/{community_id}` — authoritative revocation markers checked across replicas on cache hits and pre-emit.

### Required Firestore Security Rules (Client Denial — H6)
```javascript
match /community_source_archives/{doc} { allow read, write: if false; }
match /community_ingestion_jobs/{doc}  { allow read, write: if false; }
match /community_records/{doc}         { allow read, write: if false; }
match /community_audit_logs/{doc}      { allow read, write: if false; }
match /community_revocations/{doc}     { allow read, write: if false; }
```
Because Firebase Admin SDK bypasses client rules, restrict service-account IAM roles separately to least-privilege principals in staging and production.

## 3. Feature Flags & Configuration

All Community Knowledge flags default to **disabled / fail-closed** in `app/config.py` (read at startup by cached `get_config()`):

| Environment Variable | Default | Description |
| --- | --- | --- |
| `COMMUNITY_INGESTION_ENABLED` | `false` | Gates `/admin/community/uploads`, `/jobs`, `/review`, and approve/reject. Retraction (`action="retract"`) stays available to admins even when `false`. |
| `COMMUNITY_RAG_ENABLED` | `false` | Gates Community routing and retrieval. When `false`, exact baseline routing, tools, prompts, and persistence are preserved. |
| `COMMUNITY_FAIL_CLOSED` | `true` | On storage/retrieval failure, abstain deterministically without falling back to unverified content or official KB. |
| `COMMUNITY_SOURCE_MIN_CONFIDENCE` | `0.0` | Optional additional confidence floor (`0.0`–`1.0`); never substitutes for provenance/attestation/moderation gates. |

*(Note: unused `COMMUNITY_VECTOR_STORE_ID` was removed per L2; current retrieval uses Firestore + strict topic overlap + post-retrieval dual-gate.)*

## 4. Dual-Gate Fail-Closed Publication & Attestation Policy (C1)

Enforced at **both** moderation approval and post-retrieval (`evaluate_for_publication(..., require_published_namespace=True)`):
- Strict types: real booleans (`type(val) is bool`), string enums, safe URL parsing (rejecting malformed IPv6 brackets, `example.*`, local/private hosts, bare homepages).
- Strict ISO `publication_date` (`YYYY-MM-DD`, rejecting future dates) and strict ISO-8601 UTC signoff/capture timestamps.
- Independent server-managed `approval_attestation` binding `attestation_version == approval_version`, `approved_content_hash == compute_record_content_hash(record)`, `canonical_source_url`, `captured_publisher`, `captured_at`, and 64-hex SHA-256 `evidence_hash`.
- Re-scans `topic_ar`, `summary_sanitized_ar`, and `original_source_link` for PII and defamation signals on every evaluation.
- Imported claims (`imported_claims`) are kept strictly separate from server attestations.

## 5. Retraction, Replay & History Policy (H2 / H6)

- Retraction writes `RETRACTED` + `community_revocations/{community_id}` + `community_audit_logs` atomically, clears local cache, and broadcasts `{prefix}:community:revoked` to Redis when `COORDINATION_BACKEND=redis`.
- Retraction is monotonic (`approval_version` cannot be re-enabled by stale/regular `approve`; requires explicit `reapprove_after_retraction=true`).
- Cache hits and pre-emit delivery re-check authoritative `verify_record_not_revoked()`.
- **Replay & History Scope:** Text already delivered to a client over SSE cannot be recalled by server invalidation alone. Persisted assistant messages carry `community_source_versions` inside `citations`; on subsequent turns, `chat_service.py` omits prior assistant messages referencing any retracted Community source so retracted text cannot re-enter future prompts.

## 6. Oracle Cloud Ampere A1 Deployment & Rollback Runbook (L1)

Production runs on **Oracle Cloud Ampere A1** with **Docker (`linux/arm64`), Redis, and Caddy** (see `deploy/production/README.md`).

### Rollback Procedure
1. Keep `COMMUNITY_RAG_ENABLED=false` and `COMMUNITY_INGESTION_ENABLED=false` in the private server-side container environment file (read at startup by cached `get_config()`).
2. Drain and stop the candidate API container, preserve it under a separate diagnostic name (keep Redis AOF volume, Docker inspect output, and replay/incident evidence).
3. Restore the pinned previous ARM64 image container under the exact production name with the existing loopback port `8080` binding behind Caddy.
4. Verify loopback `/health` and `/ready`, public Caddy TLS health, one authenticated SSE canary, and one replay of its stable `X-Client-Message-ID`.
5. Never treat deleting a local git worktree or feature branch as production rollback.
