# Oracle A1 scalability release gate

Use an isolated Linux ARM64 `VM.Standard.A1.Flex` host or an already approved staging host. Record actual OCPU, RAM, OS, image digests and kernel. Build and inspect the API and Redis artifacts for `linux/arm64`. Apply this procedure only after a staging deployment is authorized.

## Configuration and preflight

1. Render `compose.scalability.yaml` with immutable image references and a protected copy of `backend.env.example`. Use a dedicated staging Firebase project and a staging web origin in `CORS_ALLOWED_ORIGINS`. Set `COORDINATION_BACKEND=redis`, a staging-only key prefix, and a password-authenticated Redis URL pointing to `127.0.0.1:16379`. Use a URL-encoded password. Avoid dumping rendered environment variables.
2. Mount `redis.scalability.conf` and a protected ACL file readable by the Redis container UID. Disable the default account. Give a named staging account access to the staging key prefix and only the Redis commands used by the application. Use separate read-only monitoring credentials for PING/INFO. Verify the ACL with the end-to-end tests. Back up the AOF volume and drill restoration on an isolated copy. Use TLS and private networking if Redis moves off-host.
3. Caddy and both container listeners share the staging host network. Expose HTTPS to the authorized tester CIDRs. Keep API port 18080 and Redis 16379 on loopback. Validate the Caddyfile and confirm HTTP/SSE traffic is routed only through the approved ingress. Caddy rebuilds forwarding headers from its peer and Uvicorn trusts only loopback. Revisit the trust chain if an OCI load balancer is added.
4. Verify approved and disallowed browser origins with OPTIONS requests for POST, Authorization and Last-Event-ID. Assert that only the approved origin gets the matching Access-Control-Allow-Origin. Exercise a real browser, as a native Flutter test does not enforce CORS. Review the actual production config separately.
5. Read the actual OpenAI model/project RPM and TPM quotas and inputs from other consumers in the project. Set positive OPENAI_RPM_BUDGET and OPENAI_TPM_BUDGET below the available quota. Verify intentional rejection against an isolated mock. Zero disables the current guard.
6. Run one worker, then two workers on one listener. For a distinct-host trial, use the same private Redis and staging Firestore, route explicitly to each instance for the reconnect check, and verify the trust boundary at each listener.

## Reproducible load procedure

Install `requirements-test.txt` and run `python scripts/validate_distributed.py --redis-server /usr/bin/redis-server --instances 2 --waves 50` on the staging A1 host. Use `--instances 1` for a single-process control. The harness uses loopback, disposable auth/AOF, the app's chat route and Redis runtime, and a deterministic mock provider. It covers two separately addressable Uvicorn processes. For a long soak, run enough waves to cover at least ten minutes per stage and collect host samples throughout. Repeat the matrix through staging Caddy with the mock fixture on a protected listener to include proxy and network overhead. Use separate browser/Flutter and guarded Firestore emulator checks for the dependencies the mock replaces.

For the shared-listener smoke, use `--instances 1 --uvicorn-workers 2 --waves 0`. To run the Flutter datasource replay test on Windows, pass `--flutter-root` and `--flutter-sdk` with the two-instance mode.

Record separate counts for policy overload versus unexpected errors, completions, throughput, successful TTFT/total percentiles, CPU, RSS, event-loop lag, Redis memory and PING latency, and provider execution count. The mock executable is loopback-only and bypasses real authentication. Keep it outside deployed application ingress and never run the load matrix against production.

Proposed acceptance gates for operator review:

- Zero duplicate upstream executions in healthy runs and zero stale writes. Crash-recovery retries with an ambiguous upstream outcome must be reported separately.
- At least 99% terminal completion for admitted streams, under 1% unexpected errors, and no duplicate terminal events. Track overload separately and require bounded, actionable rejection when the queue fills.
- P95 TTFT no more than 2× the 10-request staging baseline at the intended beta admission level. P95 loop lag under 100 ms, RSS under 75% of the host/container budget, no sustained CPU above 80%, and zero Redis evictions or rejected writes.
- Repeat the chosen passing level with Redis restart, one worker killed during streaming, and controlled reconnect to a different instance. Raise the beta limit only after results show headroom.

## Monitoring and rollback

Collect Caddy HTTP status/latency, API structured error counts and SSE terminal events, Redis latency, used memory, evictions and AOF write status, and host/container CPU/RSS. Set alerts for readiness failure, Redis write failure or eviction, increasing upstream 429s, P95 TTFT above the beta threshold, and missing SSE completion above 1%. Include a synthetic retried chat with a single upstream count in the staging smoke check. Confirm an induced failure reaches the operator and resolves after recovery. This repository does not provision an alert receiver.

Keep the previous API image, environment and Caddy configuration at hand. Drain incoming traffic, let active streams finish or hit the drain deadline, and stop all distributed workers before routing to a previous single-worker memory-mode image. Verify /health, /ready, CORS and a complete chat/retry, then reopen ingress. Preserve the Redis volume until the incident review is complete. Rehearse the sequence on staging before release.

References: [Oracle Ampere A1](https://docs.oracle.com/en-us/iaas/Content/Compute/References/arm.htm), [Redis AOF durability](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/), [Caddy proxy trust](https://caddyserver.com/docs/caddyfile/options).
