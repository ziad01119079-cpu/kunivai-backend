# Kuniv AI scalability release handover

**Scalability development CLOSED.** Oracle production release succeeded on 2026-11-24. The active API was running and healthy with zero restarts after smoke verification.

| Artifact | Identifier |
| --- | --- |
| Production Git revision | `df6c371d0936e4e152659f0169293d0436eb62a1` |
| Git release tag | `kuniv-backend-scalability-v1.0.0` |
| Production Docker digest | `sha256:b9de14329da9b86be2c7258c3ca3911a2120cc76b414cb1a9d0d9b1729aad5c3` |
| Previous production Docker digest | `sha256:212139ef60fa7750b665bb1f4d30dde82aff145647b640a4b7783164bb6ba2b4` |
| Previous container | `kunivai-api-rollback-v100`, stopped and retained |
| Private Oracle release state | `/var/lib/kuniv-release/scalability-v1.0.0` |
| ARM64 dependency lock | `deploy/production/requirements-arm64.lock` |

The release branch is `codex/distributed-scalability-architecture` on the configured `new-origin` remote. The release tag stays at the exact built revision. Documentation commits after the tag do not change the deployed image.

## Final review

| Classification | Finding and disposition |
| --- | --- |
| RELEASE BLOCKER, fixed | Published Flutter sends the initial chat without a message ID. The server now assigns one, returns it in `X-Client-Message-ID`, and uses durable ownership. Replay still requires a matching supplied ID. |
| RELEASE BLOCKER, fixed | The former branch counted generation starts and could reject inside SSE. The release atomically enforces 30 global authenticated requests and 10 per UID per rolling minute in Redis, returning HTTP 429 and Retry-After before streaming, as in the old production API. |
| RELEASE BLOCKER, fixed | The old runtime had no Redis. The release provisioned an authenticated private Redis on the existing VM, enabled AOF and verified the primary epoch from the ARM64 image. Missing or changed Redis coordination closes readiness and admission. |
| NON-BLOCKING | Legacy Flutter cannot reuse the server-assigned ID on a separate manual retry. PDF schedule deduplication still happens after parsing, so a repeated import can repeat a provider call. Both paths remain budgeted. App Check is configurable and off for compatibility. |
| NON-BLOCKING | A Redis restart requires an operator to inspect persistence, quota counters and primary identity before re-pinning the epoch. Provider calls can be externally ambiguous at crash boundaries. Automatic replay with an existing durable claim never silently dispatches a new generation. |
| FUTURE CERTIFICATION | Isolated sustained ARM64 benchmarking with independent replicas, actual OCI load balancing and Caddy TLS, managed Redis failover, alert delivery and peak Firestore latency. Capacity at 300 RPM, 1,000 daily students or 60,000 potential students is unverified. |

The API runs one worker on the existing 2-OCPU VM. Caddy still routes to the API's loopback binding on port 8080. No existing Firestore records required migration. New chat attempts can create an additive generation-claim document. The provider RPM/TPM and daily budgets are checked before actual supported wire attempts.

## Release evidence

- 196 backend tests and 11 subtests passed. The final suite covered compatibility with a missing initial ID, stable-ID replay, identical NAT fairness, both HTTP 429 budgets and Redis coordination. Existing Python/framework deprecation warnings remain.
- The first production attempt rolled back before authenticated chat because the canary expected 401 for a missing Bearer header. Caddy preflight succeeded and the candidate returned the existing FastAPI contract of 403. The rollback restored the old exact image and public health. The corrected canary succeeded with the same candidate image.
- The final smoke verified public health/readiness, the Docker health check, Redis auth and epoch, CORS preflight, missing-Bearer 403, valid Firebase auth, legacy Flutter request shape, SSE terminal completion, exact Last-Event-ID suffix and the Firestore user/assistant/claim writes. The replay incurred zero provider attempts. There were zero HTTP 429, 500 or 503 responses in the supervised chat sample and zero Docker restarts.
- The single authenticated canary exercised the existing File Search, Web Search fallback and missed-query classifier. There were three provider attempts and zero transient provider retries. The synthetic user, conversation, claim and synthetic analytics row were removed. The provider telemetry reserves token capacity and does not establish billed dollars.
- The cutover briefly reset connections during startup. The automatic rollback timer was disarmed only after health, smoke and the expected image were verified. No load or saturation tests were run during the release.

## Rollback and recovery

Run on Oracle as a sudo-capable operator.

```sh
sudo -n /bin/bash /var/lib/kuniv-release/scalability-v1.0.0/release.sh rollback
curl -fsS https://api.kunivai.online/health
```

The rollback verifies image identities, stops the replacement, restores the original container name and checks local and public health. The full pre-release inspect records and Redis credentials are stored on Oracle with restricted permissions. Keep the saved container, private release state, rollback image and Redis volume. If Redis restarts, inspect the primary and AOF recovery and verify the quota state before re-pinning as described in the production deployment README. This version does not fall back to memory mode.

## RAG follow-up

The separate local branch `codex/rag-development` starts from the deployed Git revision. Review why a simple non-university question entered the File Search → Web Search → analytics path as part of the next RAG and cost phase. Keep the existing evidence checks when evaluating routing changes.

The [scalability certification report](docs/scalability_certification.md) preserves the previous benchmark methodology and raw evidence locations. This release does not change its certification decision. Rebuild instructions are in [the production deployment README](deploy/production/README.md).

ARM64 transfer archive SHA-256: `8ba8772d5a7a59f384a32569429bd01549e7d46061d0db990ac1baa934dcd83c`.
