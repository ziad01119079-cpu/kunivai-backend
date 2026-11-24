# Zero OpenAI cost scalability benchmark

## Execution status

No Oracle capacity result was measured in this round. The known Oracle A1 host serves production. No dedicated approved A1 host or evidence of separately reserved production and fixture resources was available. A CPU cap limits the fixture's peak demand and does not reserve CPU, disk I/O or RAM for production. Saturation on that shared host was therefore skipped. No production deployments, Firestore writes or OpenAI requests occurred.

This fixture uses the distributed branch's real FastAPI /chat route, query routing, generation orchestration, Responses event parser, Redis admission, fenced SSE and replay. It substitutes an OpenAI-compatible synthetic event generator, synthetic bearer identities, and a Redis-backed synthetic store for Firebase writes. It starts each prompt in a fresh session and substitutes calendar, history and schedule dependencies. Measurements exclude real OpenAI, Firebase and public Caddy overhead. The production hotfix's independent 30-RPM/10-per-user policy is not the Redis branch's admission policy.

Local contract verification using an in-process ASGI client and fake Redis passed typed provider timeout and rate-limit errors, provider EOF, client reconnect with cursor, one mock invocation per request, and isolated assistant persistence. Static checks passed for the Compose network isolation and CLI rejection of a public target. The Linux Docker namespaces, Unix socket tunnel and measured capacity remain to be verified on the approved Oracle target.

## Set up an approved isolated A1 host

Use Linux ARM64 on an approved Oracle 2 OCPU / 12 GB environment and a separate load generator. Confirm with `uname -m`, `nproc`, `free -h`, and `docker info`. Stage the reviewed checkout, an ARM64 Redis image and the fixture image before testing. Build the fixture with:

```sh
docker build -f deploy/staging/Dockerfile.zero-bench -t kuniv-zero-bench:reviewed .
```

Run the following on the isolated host. Replace the Redis reference with the reviewed, preloaded ARM64 tag or digest. Use a fresh private directory for each resource profile.

```sh
umask 077
BENCH_PRIVATE="$(mktemp -d /var/tmp/kuniv-zero-bench.XXXXXX)"
mkdir "$BENCH_PRIVATE/socket-dir"
chmod 0777 "$BENCH_PRIVATE/socket-dir"
export KUNIV_BENCH_DIR="$BENCH_PRIVATE/socket-dir"
export KUNIV_MOCK_RUN_ID="$(cat /proc/sys/kernel/random/uuid)"
export KUNIV_BENCH_API_IMAGE=kuniv-zero-bench:reviewed
export KUNIV_BENCH_REDIS_IMAGE=redis:7-alpine
export KUNIV_BENCH_MEMORY=1g
export KUNIV_BENCH_CPUS=0.5 KUNIV_BENCH_WORKERS=1
docker compose -p kuniv-zero-bench -f deploy/staging/compose.zero-bench.yaml config --quiet
docker compose -p kuniv-zero-bench -f deploy/staging/compose.zero-bench.yaml up -d --pull never
for svc in app redis; do
  id="$(docker compose -p kuniv-zero-bench -f deploy/staging/compose.zero-bench.yaml ps -q "$svc")"
  docker inspect --format '{{.Name}} network={{.HostConfig.NetworkMode}} cpu={{.HostConfig.NanoCpus}} memory={{.HostConfig.Memory}}' "$id"
done
printf 'Run ID: %s\n' "$KUNIV_MOCK_RUN_ID"
```

Both containers must show network mode `none`. The API also refuses startup if it detects a network interface other than loopback, a TCP Redis URL or known production credential variables. No port is published from either container. No real provider or Firebase credential is needed. Share only the run ID with the load generator.

On the isolated host, use a foreground loopback TCP-to-Unix adapter:

```sh
socat TCP-LISTEN:18765,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:"$KUNIV_BENCH_DIR/api.sock"
```

On the separate load generator, open the tunnel with the reviewed SSH host-key pin:

```sh
ssh -o StrictHostKeyChecking=yes -N -L 127.0.0.1:18765:127.0.0.1:18765 operator@approved-isolated-a1
```

The client only accepts the loopback URL and checks a unique mock run marker before generating traffic. The host-side adapter has a loopback listener and no production route. API and Redis workers remain in no-network namespaces and communicate through local Unix sockets.

## Run and compare

Keep Docker memory at 1 GiB for the API and 384 MiB for Redis; Redis CPU is 0.20 CPU. Compare API caps `0.5 CPU / 1 worker`, `1 CPU / 1 worker`, and `1 CPU / 2 workers`. Recreate the fixture with a fresh directory for each profile. Preserve the same dataset, seed per corresponding stage, load generator, duration and caps. This compares only the listed profiles and does not establish performance at 4 or 8 CPUs.

Collect server resource samples in a separate terminal before starting the load. There are two container rows per sample. Prerequisites are a free loopback port, socat and a separate generator with Python/httpx installed.

```sh
ids="$(docker compose -p kuniv-zero-bench -f deploy/staging/compose.zero-bench.yaml ps -q)"
while :; do
  printf '%s\n' "$(date -u +%FT%TZ)"
  docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} memory={{.MemUsage}} pids={{.PIDs}}' $ids
  sleep 1
done
```

Run one stage at a time from the separate machine and review before increasing load:

```sh
python scripts/zero_bench_client.py --run-id "$RUN_ID" --concurrency 10 --seconds 600 --seed c10 --trace-file c10.jsonl
python scripts/zero_bench_client.py --run-id "$RUN_ID" --rate 10 --seconds 600 --seed r10 --trace-file r10.jsonl
python scripts/zero_bench_client.py --run-id "$RUN_ID" --concurrency 4 --seconds 20 --seed faults --faults --trace-file faults.jsonl
```

Run closed-loop concurrency stages 10, 25, 50, 100, and 200, and separate paced-arrival stages 10, 30, 60, and 120 RPM. The first pass keeps the existing distributed admission configuration, 20 active, 40 queued, five-second queue timeout, and the branch's IP/user guard defaults. Run the fault command separately to validate typed timeout, provider rate rejection, EOF, and a client disconnect with Last-Event-ID. Keep intentional faults out of clean-stage error rates.

After the admission-only sweep, if the approved isolated fixture has headroom, raise its configured active/queue caps and raise the fixture IP ceiling enough that the tunneled client does not mask infrastructure saturation. Keep these caps identical for the three resource profiles. The FastAPI IP limiter is process-local, so operating below that limiter is essential for a fair worker-count comparison. No comparable change is made in production. Use a unique seed for different stages and the same seed for corresponding stages across profiles. For a confirmed ceiling, repeat the last passing stage for 15 minutes after warm-up. Inspect each stage for saturation before going higher than 120 RPM.

Track completed RPM per full minute, recorded P50/P95/P99, first-vs-repeat terminal status, retry recovery, Redis and queue metrics, plus host-side CPU/RAM samples. Require zero unexpected duplicate executions, zero unresolved streams, no dropping of offered arrivals by the load generator and under 1% unexpected errors. Count planned HTTP 429 and admission SSE rejections separately. If they are frequent, label the result as an admission test rather than a supported stage. Set the safe RPM below the last stage that meets the product latency objective and has resource headroom. Keep the test fixture isolated and stop if it shares resources with production or production health changes.

## Results and student projection

| API CPU | Workers | Stable completed RPM | P95 latency | Error rate | Peak CPU | Peak RAM |
|---:|---:|---|---|---|---|---|
| 0.5 | 1 | Unmeasured | — | — | — | — |
| 1 | 1 | Unmeasured | — | — | — | — |
| 1 | 2 | Unmeasured | — | — | — | — |

Once a measured safe throughput R exists, multiply R by the factors below and round down for a conditional DAU estimate. These factors convert peak-minute throughput to a daily workload; they do not estimate real OpenAI project quotas or AI costs.

| Questions/student/day | Peak 2× | Peak 4× | Peak 8× |
|---:|---:|---:|---:|
| 3 | 240 × R | 120 × R | 60 × R |
| 5 | 144 × R | 72 × R | 36 × R |
| 10 | 72 × R | 36 × R | 18 × R |
