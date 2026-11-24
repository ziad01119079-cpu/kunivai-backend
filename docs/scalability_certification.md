# Kuniv AI scalability certification

**Decision: NOT READY for scalability certification.** The Oracle results below predate this candidate. The acceptance gate requires isolated ARM64 staging measurements, signed-client validation, a Redis recovery drill, alert delivery and rollback rehearsal.

## Architecture gap audit

| Area | Status | Evidence and residual gap |
| --- | --- | --- |
| Multi-worker chat coordination | IMPLEMENTED | `app/routers/chat.py`, `app/services/distributed_coordination.py` and `app/chat_repository.py`. Redis coalesces by UID, session and stable UUID. A durable Firestore claim prevents redispatch when Redis state disappears. |
| Multi-instance operation | IMPLEMENTED | `deploy/staging/compose.scalability.yaml`, `Caddyfile.example`. API replicas share the Redis primary and Firestore. Different hosts, TLS and a rolling cutover require staging verification. |
| Global limits and overload | IMPLEMENTED | `coordination_runtime.py` configures starts per minute, waiting time, queue depth, per-UID limits and global concurrent generations. Grants expire. Subscriber capacity and provider budgets have separate controls. Queue depth and oldest-wait age are exported. Queue-age distributions and alert delivery remain INCOMPLETE. |
| Fairness behind NAT and abuse | IMPLEMENTED for chat | The authenticated chat limiter uses Firebase UID. Schedule and notification routes have shared admission. Caddy remains an ingress control. App Check deployment, anonymous internet policy, sybil-account monitoring and moderation/admin route coverage are INCOMPLETE. |
| Provider RPM, TPM and cost | IMPLEMENTED for supported SDK calls | `provider_transport.py` reserves before the actual wire attempt. Usage estimates, daily ceilings and file upload counts are configurable. External callers, stored vector data and provider-managed tools need separate project controls and billing alerts. Exact invoice enforcement is INCOMPLETE. |
| Stable identity | IMPLEMENTED | Redis chat mode requires a canonical UUID. Flutter preserves the UUID and event cursor across reconnects. Legacy ingress assigns a UUID before generation. Older clients must be checked before cutover. |
| Non-chat provider admission | INCOMPLETE | Schedule PDF imports share provider budgets and per-UID throttles. Their stored schedule fingerprint is calculated after extraction, so retries can repeat the provider call. The chat generation concurrency cap does not cover PDF extraction. |
| Redis recovery and fencing | IMPLEMENTED with an operational recovery gate | Pinned primary epoch checks fail closed. Select a private, durable primary, enable noeviction and persistence and validate the marker after failover. Automatic quorum failover and managed-service ACL compatibility are INCOMPLETE. Redis Cluster sharding is NOT REQUIRED for the current design and is not supported by the multi-key scripts. |
| SSE replay and socket limits | IMPLEMENTED | Exact stream event cap, replay TTL, explicit gap handling, Last-Event-ID, separate bounded Redis polling pool and per-worker/global/per-UID subscriber ceilings. |
| Firestore consistency and scalable queries | IMPLEMENTED for assistant writes and chat history reads | `chat_repository.py` uses a per-message atomic create, immutable fenced assistant writes and limited recent-message queries. Peak Firestore latency, contention on per-user activity documents, claim storage lifecycle and reconciliation are INCOMPLETE. |
| Connection pooling and shutdown | IMPLEMENTED | Bounded SDK/Redis pools, API connection ceiling, drain, bounded shutdown and client closure. Set Redis maxclients from the sum of all replica pools and control-plane margin. |
| Health, metrics and tracing | INCOMPLETE | `/health`, `/ready`, private `/internal/capacity` and request correlation are available. Alert routing, multi-service tracing, Firestore dashboards and billed-cost reconciliation remain staging/operations work. |
| Autoscaling and rollback | INCOMPLETE | Private-ingress example and runbooks exist. Scale on goodput, queue delay, provider quota and PSI together. Validate alerts, traffic draining, mixed-version compatibility and rollback under test before automatic scaling. |
| Additional thread pools and LLM classification | NOT REQUIRED | Current request path uses async Redis/Firestore/HTTP. There is no measured need for more threads or an LLM classification call. Evidence-gated routes retain verification before visible answer chunks. |


At the start of this audit, distributed identity was BROKEN for callers that omitted the message ID. Cross-worker fairness was INCOMPLETE because an IP budget depended on process-local state. Distributed provider budget enforcement was INCOMPLETE for SDK retries and secondary routes. The original shared Redis pool did not isolate blocking SSE readers. These four paths received code changes in the candidate. Legacy one-worker memory mode remains available for compatibility and is ineligible for horizontal certification.
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

### Automated validation

- Backend unit and route regression suite: 196 passed, plus 11 subtests. The test environment reported deprecations from Python/framework dependencies. The queue-age test includes metric access control and excludes expired queue entries.
- Firestore emulator: one concurrent-claim and immutable-answer integration test passed. The first implementation hit a transaction lock timeout under a concurrent claim. The candidate uses Firestore atomic create and reads the winning claim on conflict.
- Guarded Auth and Firestore demo emulators: six notification integration tests passed, including stale-fence rejection. FCM and OpenAI were blocked by test fakes.
- Flutter: whole-project flutter analyze and a separate analysis of source and test directories passed with no issues. The full suite reported 90 passed and two environment-gated skips. Signed App Check and Oracle ingress tests remain outstanding.
- Caddy 2.10.2 adapted the staging Caddyfile and four Compose YAML files parsed. A real loopback run initially showed that delete-and-set forwarding rules removed both client IP headers. After fixing the example, Caddy replaced spoofed headers with the direct peer, returned 404 for the internal metrics route and flushed the synthetic first chunk at 17 ms before the terminal at 767 ms. Oracle TLS, load-balancer trust, browser CORS, Redis ACL and rollback remain staging gates.
- Local mock validation initially failed because resolving the Debian redis-server symlink selected the RDB checker. The executable name is now preserved. A separate overlay filesystem trial suffered Redis timeouts with fsync on every write. The successful comparison uses tmpfs for its disposable data. It does not measure Oracle storage.

Prior evidence is recorded in [resource efficiency](F:/fullstack-project-audit/kuniv-report-a-resource-efficiency.md), [response latency](F:/fullstack-project-audit/kuniv-report-b-response-latency.md), [engineering plan](F:/fullstack-project-audit/kuniv-report-c-engineering-plan.md) and [Oracle engineering measurements](F:/fullstack-project-audit/kuniv-next-engineering-execution.md).

### Student-visible timing evidence carried forward

Earlier matched local route measurements reduced calendar first-visible latency by 62.30 ms at the median of paired differences with identical answers and citations. In the 5, 9 and 16 second Caddy mock runs, evidence-gated routes released their first answer chunk within 1.4 ms of the final evidence-bearing event. The Flutter widget test measured first rendered admission text at 7.828, 10.752 and 17.920 seconds, including test-engine and cold widget overhead. The candidate preserves the evidence gate and distinguishes progress from verified text. The local load fixture measures SSE TTFT at its HTTP client. Current-candidate results on signed mobile devices and Oracle TLS are still unverified. [Prior timings and evidence assertions](F:/fullstack-project-audit/kuniv-next-engineering-execution.md)

### Matched Linux benchmark

One local Linux x86-64 Docker container per run, with a shared 2-CPU/2-GiB ceiling across fixture processes, loopback HTTP, Redis 8.0.2 and no external networking. All versions used the same pinned local image (sha256:a572e5c8caaab7a3482fa6a6cb814103d75759b448227f925af0a42b7be871f0), mock delay mix, workload, admission limits and random seed. Each rate had a 20-second arrival window followed by drain. The CPU figures below are mean aggregate CPU cores during that stage. Memory is sampled combined process RSS. These runs are descriptive and establish neither sustained throughput nor the benefit of adding physical resources.

| Replicas × workers | 30 offered | 60 offered | 120 offered | 250 offered | 300 offered | Completed RPM inside 300 window | P95 TTFT / total at 300 | Mean cores / peak RSS at 300 | Peak Redis sockets / queue at 300 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 × 1 | 10/10 | 20/20 | 40/40 | 67/84 | 62/100 | 111 | 5.91 / 18.65 s | 0.45 / 138.94 MiB | 53 / 12 |
| 1 × 2 | 10/10 | 20/20 | 40/40 | 65/84 | 65/100 | 117 | 5.90 / 18.08 s | 0.38 / 292.01 MiB | 44 / 11 |
| 1 × 4 | 10/10 | 20/20 | 40/40 | 65/84 | 66/100 | 120 | 5.90 / 18.06 s | 0.37 / 527.82 MiB | 56 / 11 |
| 2 × 1 | 10/10 | 20/20 | 40/40 | 66/84 | 67/100 | 120 | 5.86 / 18.00 s | 0.33 / 254.86 MiB | 45 / 11 |
| 4 × 1 | 10/10 | 20/20 | 40/40 | 67/84 | 66/100 | 120 | 5.90 / 18.04 s | 0.34 / 491.36 MiB | 55 / 11 |

At all five topologies, 120 RPM offered was the highest sampled stage with zero rejections. Each completed all 40 after drain and delivered 84 normalized RPM inside the arrival window. Across all rates there were 1,270 arrivals, 1,006 completed answers, 264 explicit capacity rejections, zero unexpected errors or interruptions, zero unresolved streams and zero duplicate mock executions. At 300 offered RPM every profile peaked at 20 active generations while Redis P95 PING was 1.36–2.16 ms and event-loop lag P95 was 0.94–1.09 ms. The visible limit was admission and waiting for mock responses. Extra workers and replicas increased memory. The small difference at 300 offered RPM does not establish sustainable scaling or justify changing the Oracle worker count.

Every topology passed disconnect replay, Redis 503 while unavailable, authenticated AOF process restart and the old-primary provider epoch check. The separate-listener configurations passed owner termination, peer takeover, exactly one observed mock execution and rejection of a stale terminal. Test status and every per-stage metric including P50, P95, P99, RSS, Redis latency, sockets, failures and queue depth are in the raw logs. [1×1](F:/fullstack-project-audit/kuniv-linux-cert-v3-1x1.log) · [1×2](F:/fullstack-project-audit/kuniv-linux-cert-v3-1x2.log) · [1×4](F:/fullstack-project-audit/kuniv-linux-cert-v3-1x4.log) · [2×1](F:/fullstack-project-audit/kuniv-linux-cert-v3-2x1.log) · [4×1](F:/fullstack-project-audit/kuniv-linux-cert-v3-4x1.log)

Use a clean source export of the reviewed commit when creating deployable images. The local matrix tested independent listeners on the same machine, without provider egress. It did not exercise cross-host Redis latency, Firestore throughput, high availability, signed production identities, Caddy TLS or real-model token billing. Repeat on isolated ARM64 with distributed ingress, independent hosts and longer windows before certifying throughput.

### Cross-worker checks with observed process IDs

The correctness-only fixture pinned independent HTTP connections as workers became ready. Two- and four-worker configurations showed the requested number of distinct process IDs. Two simultaneous requests sent to different PIDs had the same SSE sequence and exactly one mock invocation. A separate two-listener run sent graceful SIGTERM while an SSE generation was active, then replayed its exact suffix on the peer and checked replacement readiness. Flutter resumed the exact next event across two loopback backend processes, after correcting the test's HTTP override so it used real loopback sockets. The widget-render opt-in remains skipped for this candidate run and is covered by the earlier timing artifact above. [Two workers](F:/fullstack-project-audit/kuniv-linux-cert-distinct-workers-2.log) · [Four workers](F:/fullstack-project-audit/kuniv-linux-cert-distinct-workers-4.log) · [Graceful shutdown](F:/fullstack-project-audit/kuniv-linux-cert-correctness-2x1.log) · [Flutter replay](F:/fullstack-project-audit/kuniv-cert-flutter-live-replay-final.log)
