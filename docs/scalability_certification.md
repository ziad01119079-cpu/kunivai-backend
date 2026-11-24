# Kuniv AI scalability certification

**Decision: NOT READY for scalability certification.** The Oracle results below predate this candidate. The acceptance gate requires isolated ARM64 staging measurements, signed-client validation, a Redis recovery drill, alert delivery and rollback rehearsal.

## Architecture gap audit

| Area | Status | Evidence and residual gap |
| --- | --- | --- |
| Multi-worker chat coordination | IMPLEMENTED | `app/routers/chat.py`, `app/services/distributed_coordination.py` and `app/chat_repository.py`. Redis coalesces by UID, session and stable UUID. A durable Firestore claim prevents redispatch when Redis state disappears. |
| Multi-instance operation | IMPLEMENTED | `deploy/staging/compose.scalability.yaml`, `Caddyfile.example`. API replicas share the Redis primary and Firestore. Different hosts, TLS and a rolling cutover require staging verification. |
| Global limits and overload | IMPLEMENTED | `coordination_runtime.py` configures starts per minute, waiting time, queue depth, per-UID limits and global concurrent generations. Grants expire. Subscriber capacity and provider budgets have separate controls. Queue depth is exported. Queue-age telemetry is INCOMPLETE. |
| Fairness behind NAT and abuse | IMPLEMENTED for chat | The authenticated chat limiter uses Firebase UID. Schedule and notification routes have shared admission. Caddy remains an ingress control. App Check deployment, anonymous internet policy, sybil-account monitoring and moderation/admin route coverage are INCOMPLETE. |
| Provider RPM, TPM and cost | IMPLEMENTED for supported SDK calls | `provider_transport.py` reserves before the actual wire attempt. Usage estimates, daily ceilings and file upload counts are configurable. External callers, stored vector data and provider-managed tools need separate project controls and billing alerts. Exact invoice enforcement is INCOMPLETE. |
| Stable identity | IMPLEMENTED | Redis chat mode requires a canonical UUID. Flutter preserves the UUID and event cursor across reconnects. Legacy ingress assigns a UUID before generation. Older clients must be checked before cutover. |
| Redis recovery and fencing | IMPLEMENTED with an operational recovery gate | Pinned primary epoch checks fail closed. Select a private, durable primary, enable noeviction and persistence and validate the marker after failover. Automatic quorum failover and managed-service ACL compatibility are INCOMPLETE. Redis Cluster sharding is NOT REQUIRED for the current design and is not supported by the multi-key scripts. |
| SSE replay and socket limits | IMPLEMENTED | Exact stream event cap, replay TTL, explicit gap handling, Last-Event-ID, separate bounded Redis polling pool and per-worker/global/per-UID subscriber ceilings. |
| Firestore consistency and scalable queries | IMPLEMENTED for assistant writes and chat history reads | `chat_repository.py` uses a per-message atomic create, immutable fenced assistant writes and limited recent-message queries. Peak Firestore latency, contention on per-user activity documents, claim storage lifecycle and reconciliation are INCOMPLETE. |
| Connection pooling and shutdown | IMPLEMENTED | Bounded SDK/Redis pools, API connection ceiling, drain, bounded shutdown and client closure. Set Redis maxclients from the sum of all replica pools and control-plane margin. |
| Health, metrics and tracing | INCOMPLETE | `/health`, `/ready`, private `/internal/capacity` and request correlation are available. Alert routing, multi-service tracing, Firestore dashboards and billed-cost reconciliation remain staging/operations work. |
| Autoscaling and rollback | INCOMPLETE | Private-ingress example and runbooks exist. Scale on goodput, queue delay, provider quota and PSI together. Validate alerts, traffic draining, mixed-version compatibility and rollback under test before automatic scaling. |
| Additional thread pools and LLM classification | NOT REQUIRED | Current request path uses async Redis/Firestore/HTTP. There is no measured need for more threads or an LLM classification call. Evidence-gated routes retain verification before visible answer chunks. |

## Quotas and failure semantics

The owner-reported GPT-4o Build project limits are 5,000 RPM, 450,000 TPM, 50,000 IPM and 1,350,000 queued batch tokens. Batch and image execution are outside the chat budget. The app clamps configured RPM/TPM to the project ceilings. Admission uses conservative estimates for opaque provider tool input and counts retries at the transport. Staging must validate remaining-quota headers and establish appropriate headroom. App-level daily limits apply to admitted provider calls and estimated units. Use project isolation and billing alerts for other consumers and provider-managed cost. [OpenAI rate limits](https://developers.openai.com/api/docs/guides/rate-limits)

Redis owns the stream lease. Firestore atomically authorizes one logical generation per stable identity and makes the written answer immutable. If a worker dies between claiming and issuing the call, or if delivery/charging is uncertain after submission, the same ID returns an explicit uncertain outcome or replays an available result. It does not automatically dispatch again. A deliberate new user action can send a new ID and may incur another charge. Bounded application retries of explicitly retryable provider errors still consume budget. There is no exactly-once guarantee for external billing.

Replay history expires or can be invalidated by Redis primary loss. Clients receive explicit terminal/recoverable outcomes and can use saved history where persistence succeeded. Preserve generation claim documents with conversation history. A restore or purge of those claims requires a cutover plan to prevent old IDs from being reused. Validate both against a staging snapshot.

## Operational rollout and rollback

1. Validate pinned ARM64 images and compare equal-workload runs on isolated A1 with multiple host allocations. Test independent replicas with a shared Redis/Firestore emulator, a private Caddy route and no provider egress. Start with the same one-worker admission caps. Calibrate alert thresholds using goodput, queue age, TTFT, provider budget use, Redis latency and event-loop lag.
2. Provision a private persistent Redis primary and restricted ACL. Supply a single Redis prefix, epoch and policy across all replicas. Caddy must trust only the intended immediate proxies, discard untrusted forwarding headers and keep metrics private. Verify TLS/CORS in an actual browser. Configure App Check in report mode and verify signed web/mobile builds and required desktop clients before enforcement. [Firebase App Check for custom backends](https://firebase.google.com/docs/app-check/custom-resource-backend)
3. Validate Firebase permissions and restore isolated backups. Rehearse draining and stopping old writers before changing the coordination contract. Deploy the new candidate to private staging first. Avoid overlapping old writers that do not enforce durable claims with the new generation path. Preserve the previous compatible image and reviewed environment.
4. Rehearse alerts for 429/503, tail latency, Redis epoch loss, Firestore failures and quota consumption. Exercise Redis primary loss. Recovery requires draining writers, checking durable state, allowing a full provider quota window and confirming the daily budget is intact or intentionally reset by an operator. Then re-pin the current primary before enabling readiness.
5. For rollback, remove candidate ingress, stop admissions, drain/cancel workers with a bounded timeout and verify no active old writers. Restore the previous compatible image, environment and routing. Keep Firestore documents intact and retain the Redis namespace for investigation. With an older image that does not enforce durable generation claims, require explicit incident approval and verification before reopening generation. Re-run health, readiness, authentication and SSE replay probes.
6. Scale via worker count, eligible replicas, quotas, CPU/RAM and pool changes once staging measurements justify them. Stateful limits include Redis script/connection capacity, Firestore write contention and OpenAI quota. Sharding Redis coordination, adding cross-region failover or switching to priority-based multi-tenant scheduling would require design and code changes.

## Prior Oracle evidence

A previous isolated Oracle comparison held one worker and 60 offered RPM constant. Both 0.5 CPU and 1 CPU completed 125/125 at 56.64 completed RPM with zero API cgroup throttles. Doubling the quota produced no measured throughput gain in that run. The prior 1 CPU two-worker startup stopped under the PSI guard at 23.34% with 21/21 throttled periods. Prior diagnostic testing observed a separate Oracle monitoring-agent cgroup consuming about 0.40 CPU without the test fixture. That explains the pressure mechanism. It does not allocate every CPU second of the original event. The serialized-import factory mitigates simultaneous cold imports locally. The guarded Oracle startup must be rerun before recommending two workers. Prior calendar measurements saved about 62 ms locally. Admission, File Search and Web Search still carry evidence-verification latency.

## Candidate verification

### Redis epoch initialization

From the private staging network, using a restricted operator Redis connection, record the current primary's run_id from INFO server. Confirm the endpoint is the writable primary. Set <REDIS_KEY_PREFIX>:epoch to <AI_REDIS_EPOCH>:<run_id> with no TTL and keep the epoch value identical across replicas. Use a newly generated epoch for a new environment. Provision the application ACL for INFO and its required commands, and restrict epoch changes to the operator account where ACLs permit. Verify /ready on all replicas and verify that changing or deleting the marker causes HTTP 503. Do not automate re-pinning on startup. Primary restart or restoration can invalidate quota counters even if persisted replay survives.

### Reproduce local mock tests

From the backend root, build the disposable fixture and run each row independently. This runs the actual chat route with synthetic auth and simulated 5, 9 and 16 second responses. Redis is local, authenticated and isolated. For the local comparison the AOF and test claim database were on tmpfs. The run validates recovery after a process restart, with disk restore and managed failover still requiring staging. The benchmark claim adapter uses SQLite. The Firestore contract is exercised separately against its emulator.

```sh
docker build -f deploy/staging/Dockerfile.certification -t kuniv-cert:local .
docker run --rm --network none --cpus 2 --memory 2g \
  --tmpfs /tmp:rw,size=256m -e KUNIV_FIXTURE_TEMP=/tmp -e PYTHONUNBUFFERED=1 \
  kuniv-cert:local --instances 1 --uvicorn-workers 1 \
  --waves 1 --stage-seconds 20 --paced-rpm 30 60 120 250 300
```

Repeat with instance/worker pairs (1, 2), (1, 4), (2, 1) and (4, 1). Compare separately with sustained windows and repeated runs in isolated ARM64 staging, using the same fixture, budgets and random seed. Record the raw metrics and the container allocation for every row. A 60,000-identity synthetic population is sampled with a skewed distribution. The samples in these brief runs do not exercise all 60,000 identities or establish a supported daily-active-user count.
