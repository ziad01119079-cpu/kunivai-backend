"""Run a disposable, authenticated real Redis and mock-only HTTP/SSE workload."""

import argparse
import asyncio
from contextlib import AsyncExitStack, aclosing, closing
import json
import math
import os
import random
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import psutil
from redis.asyncio import Redis
from redis.exceptions import RedisError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.distributed_coordination import RedisCoordinator, RedisOpenAIBudget


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def percentiles(samples):
    ordered = sorted(samples)
    return {
        f"p{p}": round(ordered[math.ceil(p * len(ordered) / 100) - 1], 2)
        if ordered else None
        for p in (50, 95, 99)
    }


async def wait_for(probe, process):
    for _ in range(200):
        if process.poll() is not None:
            raise RuntimeError(f"Fixture exited with status {process.returncode}")
        try:
            if await probe():
                return
        except (RedisError, httpx.HTTPError):
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("Fixture startup timed out")


async def events(client, url, key, *, cursor=None, message="fixture", trace=None, uid=None):
    headers = {"x-mock-user": uid or key, "x-request-id": str(uuid4()) if trace is not None else key}
    if trace is not None:
        trace["request_id"] = headers["x-request-id"]
    if cursor:
        headers["Last-Event-ID"] = cursor
    async with client.stream(
        "POST", f"{url}/chat", headers=headers,
        json={"message": message, "sessionId": "fixture", "client_message_id": key},
    ) as response:
        worker = response.headers["X-Test-Worker"]
        if trace is not None:
            trace.update(worker=worker, http_status=response.status_code)
        response.raise_for_status()
        event_id = None
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                event_id = line[4:]
            elif line.startswith("data: "):
                yield worker, event_id, json.loads(line[6:])


async def collect(client, url, key, **kwargs):
    return [event async for event in events(client, url, key, **kwargs)]


async def benchmark(client, urls, redis, processes, concurrency, waves, trace_file, *,
                    ingress=None, stage_seconds=0, steady=False, offered_rpm=0, population=60000):
    keys = [] if steady or offered_rpm else [str(uuid4()) for _ in range(concurrency * waves)]
    redis_rtt, rss, lag = [], [], []
    redis_connected, redis_blocked = [], []
    active_depth, queue_depth = [], []
    redis_probe_failures = 0
    stop = asyncio.Event()
    cpu = lambda: sum(sum(p.cpu_times()[:2]) for p in processes)
    cpu_before = cpu()
    coordinator = RedisCoordinator(redis, prefix="kuniv-validation")

    async def recover(i, trace):
        key = trace["client_message_id"]
        uid = trace.get("synthetic_uid", key)
        meta, _, stream_key = coordinator._keys(f"{uid}:fixture:{key}")
        try:
            status, tail, count = await asyncio.gather(
                redis.hget(meta, "status"), redis.xrevrange(stream_key, count=1),
                redis.hget("kuniv-validation:mock:calls", key),
            )
            trace["redis_status_before_retry"] = status.decode() if status else None
            trace["redis_last_type"] = json.loads(tail[0][1][b"event"][6:])["type"] if tail else None
            trace["upstream_calls_before_retry"] = int(count or 0)
        except RedisError as exc:
            trace["redis_diagnostic_error"] = type(exc).__name__
        if trace["outcome"] in ("done", "AI_CAPACITY_EXCEEDED"):
            trace["terminal_already_delivered"] = True
            return
        retry = {"resume_from": trace.get("cursor")}
        trace["retry"] = retry
        try:
            async for _, _, event in events(
                client, ingress or urls[(i + 1) % len(urls)],
                key, cursor=trace.get("cursor"), trace=retry, uid=uid,
            ):
                if event["type"] in ("done", "error"):
                    retry["terminal"] = event.get("code", "done")
        except httpx.HTTPError as exc:
            retry["transport_error"] = f"{type(exc).__name__}: {exc}"

    async def sample():
        nonlocal redis_probe_failures
        while not stop.is_set():
            started = time.perf_counter()
            try:
                await redis.ping()
                redis_rtt.append((time.perf_counter() - started) * 1000)
                async with redis.pipeline(transaction=False) as pipeline:
                    pipeline.zcard("kuniv-validation:capacity:active")
                    pipeline.zcard("kuniv-validation:capacity:queue")
                    pipeline.info("clients")
                    active, waiting, info = await pipeline.execute()
                active_depth.append(active)
                queue_depth.append(waiting)
                redis_connected.append(info["connected_clients"])
                redis_blocked.append(info["blocked_clients"])
            except RedisError:
                redis_probe_failures += 1
            rss.append(sum(p.memory_info().rss for p in processes))
            await asyncio.sleep(0.05)

    async def request(i, key, uid=None):
        started = time.perf_counter()
        ttft = None
        terminal = "missing_done"
        trace = {"client_message_id": key}
        if uid is not None:
            trace["synthetic_uid"] = uid
        try:
            async for _, cursor, event in events(client, ingress or urls[i % len(urls)], key, trace=trace, uid=uid):
                trace.update(cursor=cursor, last_event=event["type"])
                if event["type"] == "chunk" and ttft is None:
                    ttft = (time.perf_counter() - started) * 1000
                if event["type"] == "done":
                    terminal = "done" if terminal == "missing_done" else "duplicate_done"
                if event["type"] == "error":
                    terminal = event["code"]
        except httpx.HTTPError as exc:
            trace["transport_error"] = f"{type(exc).__name__}: {exc}"
            trace["transport_causes"] = []
            cause = exc.__cause__
            while cause is not None:
                trace["transport_causes"].append(repr(cause))
                cause = cause.__cause__
            if terminal == "missing_done":
                terminal = type(exc).__name__
        completed_at = time.perf_counter()
        trace["outcome"] = terminal
        trace["interrupted"] = (
            terminal in ("missing_done", "AI_COORDINATION_UNAVAILABLE")
            or ("transport_error" in trace and terminal != "HTTPStatusError")
        )
        if (steady or offered_rpm) and trace["interrupted"]:
            await recover(i, trace)
        return ttft, (completed_at - started) * 1000, terminal, trace, completed_at

    sampler = asyncio.create_task(sample())
    started = time.perf_counter()
    results = []
    deadline = started + stage_seconds
    dropped = 0

    async def lane(i):
        while time.perf_counter() < deadline:
            result = await request(i, str(uuid4()))
            results.append(result)
            if result[2] != "done":
                await asyncio.sleep(0.5)

    try:
        wave = 0
        if offered_rpm:
            interval = 60 / offered_rpm
            rng = random.Random(20261124)
            pending = set()
            for i in range(math.ceil(stage_seconds / interval)):
                await asyncio.sleep(max(0, started + i * interval - time.perf_counter()))
                if time.perf_counter() >= deadline:
                    break
                if len(pending) >= concurrency:
                    dropped += 1
                    continue
                # A small repeat-use cohort receives 25% of traffic. Protocol
                # IDs remain unique even when a user sends several questions.
                cohort = max(1, population // 20) if rng.random() < .25 else population
                uid = f"student-{rng.randrange(cohort)}"
                key = str(uuid5(NAMESPACE_URL, f"kuniv-cert:{offered_rpm}:{i}"))

                async def record(index, identifier, student):
                    results.append(await request(index, identifier, student))

                task = asyncio.create_task(record(i, key, uid))
                pending.add(task)
                task.add_done_callback(pending.discard)
            await asyncio.gather(*pending)
            keys = [row[3]["client_message_id"] for row in results]
        elif steady:
            await asyncio.gather(*(lane(i) for i in range(concurrency)))
            keys = [row[3]["client_message_id"] for row in results]
        while not (steady or offered_rpm) and (wave < waves or time.perf_counter() - started < stage_seconds):
            batch = keys[wave * concurrency:(wave + 1) * concurrency]
            if not batch:
                batch = [str(uuid4()) for _ in range(concurrency)]
                keys.extend(batch)
            results.extend(await asyncio.gather(*(request(i, key) for i, key in enumerate(batch))))
            wave += 1
    finally:
        stop.set()
        await sampler
    elapsed = time.perf_counter() - started
    cpu_used = cpu() - cpu_before
    memory = await redis.info("memory")
    for url in urls:
        lag.extend((await client.get(f"{url}/__mock/stats")).json()["lag_ms"][-math.ceil(elapsed / 0.02):])
    calls = await redis.hmget("kuniv-validation:mock:calls", keys)
    recovered_done = 0
    recovered_failure = 0
    unresolved = 0
    interrupted = 0
    for i, (_, _, outcome, trace, _) in enumerate(results):
        if trace["interrupted"]:
            interrupted += 1
            if not (steady or offered_rpm):
                await recover(i, trace)
                calls[i] = await redis.hget("kuniv-validation:mock:calls", keys[i])
            retry = trace.get("retry", {})
            if retry.get("terminal") == "done":
                recovered_done += 1
            elif "terminal" in retry:
                recovered_failure += 1
            elif not trace.get("terminal_already_delivered"):
                unresolved += 1
        trace["final_upstream_calls"] = int(calls[i] or 0)
        trace["upstream_calls"] = trace.get("upstream_calls_before_retry", trace["final_upstream_calls"])
        if trace_file:
            with trace_file.open("a", encoding="utf-8") as output:
                output.write(json.dumps({"concurrency": concurrency, **trace}) + "\n")
    outcomes = {outcome: sum(row[2] == outcome for row in results) for outcome in {r[2] for r in results}}
    completed = [row for row in results if row[2] == "done"]
    minute_bins = [0] * int(stage_seconds // 60) if steady or offered_rpm else []
    for *_, completed_at in completed:
        minute = int((completed_at - started) // 60)
        if 0 <= minute < len(minute_bins):
            minute_bins[minute] += 1
    return {
        "concurrency": concurrency, "requests": len(keys), "outcomes": outcomes,
        "offered_rpm": offered_rpm or None,
        "identity_population": population if offered_rpm else None,
        "unique_sampled_identities": len({row[3].get("synthetic_uid") for row in results}) if offered_rpm else None,
        "dropped_arrivals": dropped,
        "completed_rpm_in_offer_window": round(
            60 * sum(row[-1] < deadline for row in completed) / stage_seconds, 2,
        ) if offered_rpm else None,
        "elapsed_seconds": round(elapsed, 2),
        "interrupted": interrupted, "recovered_done": recovered_done,
        "recovered_failure": recovered_failure, "unresolved": unresolved,
        "completed_per_second": round(len(completed) / elapsed, 2),
        "successful_requests_per_minute_including_drain": round(60 * len(completed) / elapsed, 2),
        "successful_requests_by_full_minute": minute_bins,
        "capacity_rejections": outcomes.get("AI_CAPACITY_EXCEEDED", 0),
        "error_rate": round(1 - len(completed) / len(keys), 4),
        "unexpected_errors": len(keys) - len(completed) - outcomes.get("AI_CAPACITY_EXCEEDED", 0),
        "ttft_ms": percentiles([row[0] for row in completed if row[0] is not None]),
        "total_ms": percentiles([row[1] for row in completed]),
        "loop_lag_ms": percentiles(lag), "redis_ping_ms": percentiles(redis_rtt),
        "redis_probe_failures": redis_probe_failures,
        "redis_peak_connected": max(redis_connected, default=None),
        "redis_peak_blocked": max(redis_blocked, default=None),
        "peak_active_generations": max(active_depth, default=None),
        "peak_queued_requests": max(queue_depth, default=None),
        "queue_depth_percentiles": percentiles(queue_depth),
        "peak_combined_rss_mib": round(max(rss) / 2**20, 2),
        "combined_cpu_cores": round(cpu_used / elapsed, 2),
        "redis_used_memory_mib": round(memory["used_memory"] / 2**20, 2),
        "duplicate_upstream_executions": sum(max(0, int(call or 0) - 1) for call in calls),
    }


async def run(args, directory):
    redis_port = free_port()
    password = secrets.token_hex(24)
    redis_cfg = directory / "redis.conf"
    redis_cfg.write_text(
        f'bind 127.0.0.1\nport {redis_port}\nprotected-mode yes\n'
        f'appendonly yes\nappendfsync always\nrequirepass {password}\n'
        'maxmemory 128mb\nmaxmemory-policy noeviction\n', encoding="ascii",
    )
    redis_url = f"redis://:{password}@127.0.0.1:{redis_port}/0"
    redis = Redis.from_url(redis_url, socket_connect_timeout=0.3, socket_timeout=2)
    run_id = str(uuid4())
    claims_db = directory / "claims.sqlite3"
    with closing(sqlite3.connect(claims_db)) as db, db:
        db.execute("CREATE TABLE claims(uid, session, message, fingerprint, PRIMARY KEY(uid, session, message))")
    env = dict(
        os.environ, KUNIV_LOCAL_MOCK="1", COORDINATION_BACKEND="redis",
        KUNIV_MOCK_RUN_ID=run_id,
        KUNIV_MOCK_CLAIMS_DB=str(claims_db),
        REDIS_URL=redis_url, REDIS_KEY_PREFIX="kuniv-validation",
        AI_REQUEST_LEASE_MS="1500", AI_CAPACITY_LEASE_MS="1800",
        AI_MAX_CONCURRENT_GENERATIONS=str(args.active),
        AI_MAX_QUEUED_GENERATIONS=str(args.queued),
        AI_QUEUE_TIMEOUT_SECONDS=os.environ.get("AI_QUEUE_TIMEOUT_SECONDS", "2"),
        AI_IP_RATE_LIMIT="1000000/minute", AI_USER_RATE_LIMIT_REQUESTS="100",
        REDIS_SOCKET_TIMEOUT_SECONDS="10",
    )
    children = []
    with (directory / "process.log").open("wb") as log:
        def start(command, cwd):
            child = subprocess.Popen(
                command, cwd=cwd, env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            children.append(child)
            return child

        def stop_process(child):
            if child.poll() is not None:
                return
            descendants = psutil.Process(child.pid).children(recursive=True)
            child.kill()
            child.wait(timeout=5)
            for descendant in descendants:
                try:
                    descendant.kill()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(descendants, timeout=5)

        def start_redis():
            return start([str(args.redis_server), redis_cfg.name], directory)

        def start_api(port):
            return start([sys.executable, "-m", "uvicorn", "app.worker_bootstrap:create_http_fixture_app", "--factory",
                          "--host", "127.0.0.1", "--port", str(port), "--no-access-log",
                          "--workers", str(args.uvicorn_workers)], ROOT)

        try:
            server = start_redis()
            await wait_for(redis.ping, server)
            assert (await redis.info("persistence"))["aof_enabled"] == 1
            budget = RedisOpenAIBudget(redis, prefix="epoch-test", rpm=2, tpm=12, epoch=run_id)
            await redis.set(budget.epoch_key, f"{run_id}:{(await redis.info('server'))['run_id']}")
            assert await budget.reserve(6) is None
            async with Redis(host="127.0.0.1", port=redis_port) as unauth:
                try:
                    await unauth.ping()
                except RedisError as exc:
                    assert "Authentication required" in str(exc)
                else:
                    raise AssertionError("Unauthenticated Redis access succeeded")

            ports = [free_port() for _ in range(args.instances)]
            if args.mock_port:
                ports[0] = args.mock_port
            urls = [f"http://127.0.0.1:{port}" for port in ports]
            apis = [start_api(port) for port in ports]
            async with httpx.AsyncClient(timeout=15, trust_env=False, limits=httpx.Limits(
                max_connections=220, max_keepalive_connections=args.keepalive_connections,
            )) as client:
                async def ready(url):
                    return (await client.get(f"{url}/__mock/stats")).is_success

                for url, api in zip(urls, apis):
                    await wait_for(lambda: ready(url), api)
                if args.uvicorn_workers > 1 and args.waves:
                    async with httpx.AsyncClient(
                        trust_env=False, limits=httpx.Limits(max_keepalive_connections=0),
                    ) as probe_client:
                        worker_ids = set()
                        deadline = time.monotonic() + 90
                        while time.monotonic() < deadline:
                            replies = await asyncio.gather(*(
                                probe_client.get(f"{urls[0]}/__mock/stats") for _ in range(16)
                            ))
                            worker_ids.update(reply.headers["X-Test-Worker"] for reply in replies)
                            if len(worker_ids) == args.uvicorn_workers:
                                break
                            await asyncio.sleep(0.25)
                        assert len(worker_ids) == args.uvicorn_workers
                    print(f"PASS shared Uvicorn listener served by {len(worker_ids)} worker PIDs", flush=True)
                if args.uvicorn_workers > 1 and not args.waves:
                    async with AsyncExitStack() as stack:
                        by_worker = {}
                        deadline = time.monotonic() + 90
                        target = f"{urls[0]}/__mock/stats"
                        while len(by_worker) < args.uvicorn_workers and time.monotonic() < deadline:
                            peers = [httpx.AsyncClient(
                                trust_env=False, limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                            ) for _ in range(16)]
                            probes = await asyncio.gather(*(peer.get(target) for peer in peers), return_exceptions=True)
                            for probe, peer in zip(probes, peers):
                                pid = None if isinstance(probe, BaseException) else probe.headers.get("X-Test-Worker")
                                if pid and pid not in by_worker:
                                    by_worker[pid] = peer
                                    stack.push_async_callback(peer.aclose)
                                else:
                                    await peer.aclose()
                            await asyncio.gather(*(peer.get(target) for peer in by_worker.values()))
                            await asyncio.sleep(0.25)
                        assert len(by_worker) == args.uvicorn_workers, "Could not pin distinct worker connections"
                        retry_key = str(uuid4())
                        pair = await asyncio.gather(*(
                            collect(peer, urls[0], retry_key) for peer in list(by_worker.values())[:2]
                        ))
                        assert pair[0][0][0] != pair[1][0][0]
                        assert pair[0][-1][2]["type"] == "done"
                        assert [row[1:] for row in pair[0]] == [row[1:] for row in pair[1]]
                        assert int(await redis.hget("kuniv-validation:mock:calls", retry_key)) == 1
                    print(f"PASS {len(by_worker)} distinct worker PIDs and cross-worker coalescing", flush=True)
                key = str(uuid4())
                left, right = await asyncio.gather(*(collect(client, url, key) for url in (urls[0], urls[-1])))
                assert left and left[-1][2]["type"] == "done", (left, right)
                assert [row[1:] for row in left] == [row[1:] for row in right]
                if len(urls) > 1:
                    assert left[0][0] != right[0][0]
                assert int(await redis.hget("kuniv-validation:mock:calls", key)) == 1
                resumed = await collect(client, urls[-1], key, cursor=left[0][1])
                assert [row[1:] for row in resumed] == [row[1:] for row in left[1:]]
                print("PASS coalescing and cross-process replay", flush=True)
                disconnect_key = str(uuid4())
                async with aclosing(events(client, urls[0], disconnect_key)) as stream:
                    _, cursor, initial = await anext(stream)
                    assert initial["content"] == "first"
                resumed_live = await collect(client, urls[-1], disconnect_key, cursor=cursor)
                assert [item[2]["type"] for item in resumed_live] == ["chunk"] * 8 + ["done"]
                assert int(await redis.hget("kuniv-validation:mock:calls", disconnect_key)) == 1
                print("PASS intentional client disconnect and exact suffix replay without regeneration", flush=True)
                if len(urls) > 1:
                    crash_key = str(uuid4())
                    async with aclosing(events(client, urls[0], crash_key, message="kill-owner")) as stream:
                        _, cursor, _ = await anext(stream)
                        stale = RedisCoordinator(redis, prefix="kuniv-validation")
                        request_key = f"{crash_key}:fixture:{crash_key}"
                        meta, _, _ = stale._keys(request_key)
                        owner, fence = await redis.hmget(meta, "owner", "fence")
                        stale.owner = owner.decode()
                        apis[0].kill()
                        apis[0].wait(timeout=5)
                    recovered = await collect(client, urls[1], crash_key, cursor=cursor, message="kill-owner")
                    assert recovered[0][2]["type"] == "reset"
                    assert recovered[-1][2]["code"] == "GENERATION_OUTCOME_UNKNOWN"
                    assert int(await redis.hget("kuniv-validation:mock:calls", crash_key)) == 1
                    assert not await stale._publish(request_key, int(fence), 'data: {"type":"done"}\n\n')
                    apis[0] = start_api(ports[0])
                    await wait_for(lambda: ready(urls[0]), apis[0])
                    print("PASS worker kill, takeover reset, no second generation and stale terminal rejected", flush=True)
                    if os.name == "posix" and not args.waves:
                        drain_key = str(uuid4())
                        async with aclosing(events(client, urls[0], drain_key)) as stream:
                            _, drain_cursor, _ = await anext(stream)
                            apis[0].terminate()
                        assert await asyncio.to_thread(apis[0].wait, 15) in (0, -15)
                        drained = await collect(client, urls[-1], drain_key, cursor=drain_cursor)
                        assert [item[2]["type"] for item in drained] == ["chunk"] * 8 + ["done"]
                        assert int(await redis.hget("kuniv-validation:mock:calls", drain_key)) == 1
                        apis[0] = start_api(ports[0])
                        await wait_for(lambda: ready(urls[0]), apis[0])
                        print("PASS graceful SIGTERM, peer replay and replacement listener readiness", flush=True)
                interrupted_key = str(uuid4())
                async with aclosing(events(client, urls[0], interrupted_key, message="kill-owner")) as stream:
                    _, interrupted_cursor, initial = await anext(stream)
                    assert initial["type"] == "chunk"
                    server.kill()
                    server.wait(timeout=5)
                    outage = await client.post(
                        f"{urls[0]}/chat",
                        json={"message": "fixture", "sessionId": "fixture", "client_message_id": str(uuid4())},
                    )
                    try:
                        _, _, event = await anext(stream)
                    except (StopAsyncIteration, httpx.TransportError):
                        pass
                    else:
                        assert event["code"] == "AI_COORDINATION_UNAVAILABLE"
                print(f"Redis outage HTTP status: {outage.status_code}", flush=True)
                assert outage.status_code == 503
                await asyncio.sleep(1.6)
                server = start_redis()
                await wait_for(redis.ping, server)
                try:
                    await budget.reserve(1)
                except RedisError:
                    print("PASS provider epoch fail-closed across Redis restart", flush=True)
                else:
                    raise AssertionError("Restored stale epoch authorized provider traffic")
                replay = await collect(client, urls[-1], key)
                assert [row[1:] for row in replay] == [row[1:] for row in left]
                print("PASS authenticated AOF restart and completed-response replay", flush=True)
                recovered = await collect(client, urls[-1], interrupted_key,
                                          cursor=interrupted_cursor, message="kill-owner")
                assert recovered[0][2]["type"] == "reset"
                assert recovered[-1][2]["code"] == "GENERATION_OUTCOME_UNKNOWN"
                assert int(await redis.hget("kuniv-validation:mock:calls", interrupted_key)) == 1
                print("PASS Redis loss during SSE and reconnect after lease expiry", flush=True)
                if args.flutter_root:
                    assert len(urls) == 2, "Flutter replay test requires two instances"
                    checked = await asyncio.to_thread(subprocess.run, [
                        str(args.flutter_sdk / "bin/cache/dart-sdk/bin/dart.exe"),
                        str(args.flutter_sdk / "bin/cache/flutter_tools.snapshot"),
                        "--no-version-check", "test", "--no-pub", "--reporter", "expanded",
                        f"--dart-define=KUNIV_REPLAY_WORKER_A={urls[0]}",
                        f"--dart-define=KUNIV_REPLAY_WORKER_B={urls[1]}",
                        "test/chat_cross_worker_replay_test.dart",
                    ], cwd=args.flutter_root, timeout=300)
                    checked.check_returncode()
                if args.waves == 0:
                    return
                if not args.stress_short_leases:
                    await redis.config_set("appendfsync", "everysec")
                    env.update(AI_REQUEST_LEASE_MS="30000", AI_CAPACITY_LEASE_MS="120000")
                    if args.steady or args.paced_rpm:
                        env["KUNIV_MOCK_PROFILE"] = "student-mix"
                    for api in apis:
                        stop_process(api)
                    apis = [start_api(port) for port in ports]
                    for url, api in zip(urls, apis):
                        await wait_for(lambda: ready(url), api)
                processes = [psutil.Process(p.pid) for p in [server, *apis]]
                if args.uvicorn_workers > 1:
                    processes.extend(psutil.Process(apis[0].pid).children(recursive=True))
                for level in args.paced_rpm or args.levels:
                    if args.ingress:
                        probe = await client.get(f"{args.ingress}/__mock/stats")
                        probe.raise_for_status()
                        if probe.json().get("run_id") != run_id:
                            raise RuntimeError("Ingress did not reach this disposable mock fixture")
                    print(json.dumps(await benchmark(
                        client, urls, redis, processes, 200 if args.paced_rpm else level, args.waves, args.trace_file,
                        ingress=args.ingress, stage_seconds=args.stage_seconds,
                        steady=args.steady, offered_rpm=level if args.paced_rpm else 0,
                        population=args.identity_population,
                    )), flush=True)
        except Exception:
            log.flush()
            diagnostics = (directory / "process.log").read_text(errors="replace")
            print(diagnostics[-3500:].replace(password, "[redacted]"), file=sys.stderr)
            raise
        finally:
            await redis.aclose()
            for child in reversed(children):
                stop_process(child)
            if args.trace_file:
                args.trace_file.with_suffix(".server.log").write_text(
                    (directory / "process.log").read_text(errors="replace").replace(password, "[redacted]"),
                    encoding="utf-8",
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    # Debian Redis selects its server or checker mode from the executable name.
    # Preserve the redis-server symlink when the package uses a shared binary.
    parser.add_argument("--redis-server", required=True, type=lambda p: Path(p).absolute())
    parser.add_argument("--instances", type=int, choices=(1, 2, 4), default=2)
    parser.add_argument("--uvicorn-workers", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument("--active", type=int, default=20)
    parser.add_argument("--queued", type=int, default=40)
    parser.add_argument("--waves", type=int, default=3)
    parser.add_argument("--levels", type=int, nargs="+", default=(10, 25, 50, 100, 200))
    parser.add_argument("--trace-file", type=Path)
    parser.add_argument("--stage-seconds", type=float, default=0)
    parser.add_argument("--steady", action="store_true", help="Use continuous load and the student streaming mix")
    parser.add_argument("--paced-rpm", type=int, nargs="+")
    parser.add_argument("--identity-population", type=int, default=60_000)
    parser.add_argument("--ingress", help="Approved HTTPS origin routed only to this disposable fixture")
    parser.add_argument("--mock-port", type=int, help="Staging-only loopback port for mock ingress")
    parser.add_argument("--keepalive-connections", type=int, default=220)
    parser.add_argument("--stress-short-leases", action="store_true")
    parser.add_argument("--flutter-root", type=Path)
    parser.add_argument("--flutter-sdk", type=Path)
    args = parser.parse_args()
    if not args.redis_server.is_file():
        parser.error("--redis-server must point to a Redis executable")
    if args.flutter_root and not args.flutter_sdk:
        parser.error("--flutter-root requires --flutter-sdk")
    if args.uvicorn_workers > 1 and args.instances != 1:
        parser.error("Shared-listener mode requires --instances 1")
    if args.stage_seconds < 0 or args.waves < 0 or (args.stage_seconds and not args.waves):
        parser.error("Timed benchmarks require positive --waves and --stage-seconds")
    if args.steady and (not args.stage_seconds or args.stress_short_leases):
        parser.error("--steady requires --stage-seconds and normal generation leases")
    if args.paced_rpm and (
        args.steady or not args.stage_seconds or args.stress_short_leases
        or min(args.paced_rpm) < 1 or args.identity_population < 1
        or len(set(args.paced_rpm)) != len(args.paced_rpm)
    ):
        parser.error("--paced-rpm needs a positive stage duration, population, and distinct rates")
    if args.ingress:
        origin = urlsplit(args.ingress)
        if (origin.scheme != "https" or not origin.hostname or origin.username is not None
                or origin.path not in ("", "/") or origin.query or origin.fragment
                or args.instances != 1 or not args.mock_port or not 1024 <= args.mock_port <= 65535
                or args.waves < 1):
            parser.error("Ingress requires an approved HTTPS origin, --instances 1, --mock-port and positive --waves")
        args.ingress = args.ingress.rstrip("/")
    with tempfile.TemporaryDirectory(prefix="kuniv-scale-", dir=os.getenv("KUNIV_FIXTURE_TEMP", ROOT)) as name:
        directory = Path(name)
        asyncio.run(run(args, directory))
