"""Run a zero-cost stage from a separate machine through a loopback SSH tunnel."""

import argparse
import asyncio
from collections import Counter
import json
import math
import time
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx


QUESTIONS = (
    "اشرح الانحدار الخطي مع مثال.",
    "ما شروط القبول في جامعة الكويت؟",
    "اشرح بالتفصيل كيف أحل معادلة تربيعية.",
)


def pct(values):
    values = sorted(values)
    return {f"p{p}": round(values[math.ceil(len(values) * p / 100) - 1], 2)
            for p in (50, 95, 99)} if values else {}


def sampled_loop_lag(samples):
    """Summarize rolling windows separately for each sampled worker."""
    workers = {}
    for sample in samples:
        values = sample.get("loop_lag_ms")
        if not values:
            continue
        worker = workers.setdefault(sample["worker"], {
            "probe_count": 0, "max_rolling_p95_ms": 0.0, "max_ms": 0.0,
        })
        worker["probe_count"] += 1
        worker["max_rolling_p95_ms"] = max(worker["max_rolling_p95_ms"], pct(values)["p95"])
        worker["max_ms"] = max(worker["max_ms"], round(max(values), 2))
    return workers


class Stage:
    def __init__(self, client, args):
        self.client, self.args = client, args
        self.results = []
        self.samples = []
        self.stop = asyncio.Event()
        self.abort_reason = None
        self.sequence = 0
        self.generator_saturated = 0

    async def probe(self):
        response = await self.client.get("/__mock/stats", headers={"Connection": "close"}, timeout=5)
        response.raise_for_status()
        result = response.json()
        if (result.get("run_id") != self.args.run_id or result.get("mock") is not True
                or result.get("egress") != "unix-only"):
            raise ValueError("Tunnel is not connected to the approved isolated fixture")
        return result

    async def sample(self):
        while not self.stop.is_set():
            try:
                self.samples.append(await self.probe())
            except (httpx.HTTPError, ValueError):
                self.abort_reason = "fixture probe failed"
                self.stop.set()
                return
            await asyncio.sleep(1)

    async def request(self, sequence):
        key = str(uuid5(NAMESPACE_URL, f"{self.args.seed}:{sequence}"))
        fault = ("timeout", "rate", "eof", "disconnect")[sequence % 4] if self.args.faults else None
        question = QUESTIONS[sequence % len(QUESTIONS)]
        if fault and fault != "disconnect":
            question = f"[bench:{fault}] " + question
        body = {"message": question, "sessionId": key, "client_message_id": key}
        headers = {
            "Authorization": "Bearer benchmark-fixture",
            "X-Bench-User": f"bench-{sequence % self.args.users}",
            "X-Request-ID": key,
        }
        started = time.perf_counter()
        cursor = None
        result = {"key": key, "fault": fault, "chunks": 0, "interrupted": False, "terminal": None}
        for attempt in (0, 1):
            if cursor:
                headers["Last-Event-ID"] = cursor
            try:
                async with self.client.stream("POST", "/chat", headers=headers, json=body) as response:
                    result[f"http_{attempt}"] = response.status_code
                    if response.status_code != 200:
                        result["terminal"] = f"HTTP_{response.status_code}"
                        break
                    pending_id = None
                    async for line in response.aiter_lines():
                        if line.startswith("id: "):
                            pending_id = line[4:]
                            continue
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        cursor = pending_id or cursor
                        if event["type"] == "reset":
                            result["chunks"] = 0
                        elif event["type"] == "chunk":
                            result.setdefault("ttft_ms", (time.perf_counter() - started) * 1000)
                            result["chunks"] += 1
                            if fault == "disconnect" and attempt == 0:
                                break
                        elif event["type"] in ("done", "error"):
                            if result["terminal"]:
                                result["terminal"] = "DUPLICATE_TERMINAL"
                                break
                            result["terminal"] = event.get("code", event["type"])
                    if result["terminal"]:
                        break
                result["interrupted"] = True
            except (httpx.TransportError, ValueError):
                result["interrupted"] = True
        result["finish"] = time.perf_counter()
        result["total_ms"] = (result["finish"] - started) * 1000
        if result["terminal"] == "done" and result["chunks"] == 0:
            result["terminal"] = "EMPTY_DONE"
        self.results.append(result)

    async def lane(self, deadline):
        while time.perf_counter() < deadline and not self.stop.is_set():
            sequence = self.sequence
            self.sequence += 1
            await self.request(sequence)

    async def paced(self, deadline):
        pending = set()
        scheduled = time.perf_counter()
        while scheduled < deadline and not self.stop.is_set():
            await asyncio.sleep(max(0, scheduled - time.perf_counter()))
            if time.perf_counter() >= deadline or self.stop.is_set():
                break
            # Do not bunch late sends together if the generator is overloaded.
            interval = 60 / self.args.rate
            if time.perf_counter() - scheduled >= interval or len(pending) >= self.args.max_inflight:
                self.generator_saturated += 1
            else:
                task = asyncio.create_task(self.request(self.sequence))
                self.sequence += 1
                pending.add(task)
                task.add_done_callback(pending.discard)
            scheduled += interval
        await asyncio.gather(*pending)

    async def run(self):
        await self.probe()
        sampler = asyncio.create_task(self.sample())
        started = time.perf_counter()
        deadline = started + self.args.seconds
        try:
            if self.args.rate:
                await self.paced(deadline)
            else:
                await asyncio.gather(*(self.lane(deadline) for _ in range(self.args.concurrency)))
        finally:
            self.stop.set()
            await sampler

        keys = [row["key"] for row in self.results]
        calls = []
        for i in range(0, len(keys), 250):
            response = await self.client.post("/__mock/calls", json=keys[i:i + 250])
            response.raise_for_status()
            calls.extend(response.json())
        done = [row for row in self.results if row["terminal"] == "done"]
        outcomes = Counter(row["terminal"] or "UNRESOLVED" for row in self.results)
        rejections = outcomes["HTTP_429"] + outcomes["AI_CAPACITY_EXCEEDED"]
        unexpected = sum(
            row["terminal"] not in {"done", "HTTP_429", "AI_CAPACITY_EXCEEDED"}
            for row in self.results
        )
        minutes = [0] * int(self.args.seconds // 60)
        for row in done:
            minute = int((row["finish"] - started) // 60)
            if minute < len(minutes):
                minutes[minute] += 1
        print(json.dumps({
            "run_id": self.args.run_id, "concurrency": self.args.concurrency, "offered_rpm": self.args.rate,
            "stage_seconds": self.args.seconds, "attempted": len(keys), "outcomes": outcomes,
            "completed": len(done), "capacity_or_policy_rejected": rejections,
            "unexpected_error_rate": unexpected / len(keys) if keys else None,
            "completed_by_full_minute": minutes,
            "completed_rpm_during_window": round(
                60 * sum(row["finish"] < deadline for row in done) / self.args.seconds, 2),
            "ttft_ms": pct([row["ttft_ms"] for row in done if "ttft_ms" in row]),
            "total_ms": pct([row["total_ms"] for row in done]),
            "interrupted": sum(row["interrupted"] for row in self.results),
            "recovered_to_done": sum(row["interrupted"] for row in done),
            "unresolved": outcomes["UNRESOLVED"],
            "duplicate_upstream": sum(max(0, n - 1) for n in calls),
            "peak_queued": max((s["queued"] for s in self.samples), default=None),
            "peak_active": max((s["active"] for s in self.samples), default=None),
            "redis_ping_ms": pct([s["redis_ping_ms"] for s in self.samples]),
            "redis_peak_memory_bytes": max((s["redis_memory_bytes"] for s in self.samples), default=None),
            "redis_peak_connected_clients": max(
                (s["redis_connected_clients"] for s in self.samples if "redis_connected_clients" in s),
                default=None,
            ),
            "redis_peak_blocked_clients": max(
                (s["redis_blocked_clients"] for s in self.samples if "redis_blocked_clients" in s),
                default=None,
            ),
            "worker_pids_sampled": sorted({s["worker"] for s in self.samples}),
            "event_loop_lag_by_sampled_worker": sampled_loop_lag(self.samples),
            "generator_dropped_arrivals": self.generator_saturated,
            "aborted": self.abort_reason,
        }), flush=True)
        if self.args.trace_file:
            with open(self.args.trace_file, "x", encoding="utf-8") as output:
                for row, count in zip(self.results, calls):
                    output.write(json.dumps({**row, "mock_calls": count}) + "\n")
        clean_stage_failed = not self.args.faults and (
            not keys or not done or unexpected / len(keys) >= 0.01
        )
        return int(bool(
            self.abort_reason or outcomes["UNRESOLVED"] or any(n > 1 for n in calls)
            or self.generator_saturated or clean_stage_failed
        ))


async def verify_workers(stage, expected):
    workers = set()
    for _ in range(20):
        workers.add((await stage.probe())["worker"])
        if len(workers) == expected:
            break
    assert len(workers) == expected, f"Expected {expected} distinct worker PIDs, saw {workers}"

    request_id = str(uuid5(NAMESPACE_URL, f"{stage.args.run_id}:coalescing"))
    limited_id = str(uuid5(NAMESPACE_URL, f"{stage.args.run_id}:rate-limit"))

    async def stream(key, uid, cursor=None, interrupt=False):
        headers = {
            "Authorization": "Bearer benchmark-fixture",
            "X-Bench-User": uid,
            "Connection": "close",
        }
        if cursor:
            headers["Last-Event-ID"] = cursor
        async with stage.client.stream(
            "POST", "/chat", headers=headers,
            json={"sessionId": key, "client_message_id": key, "message": QUESTIONS[0]},
        ) as response:
            if response.status_code != 200:
                await response.aread()
                return response.status_code, response.json()
            payloads = []
            event_id = None
            async for line in response.aiter_lines():
                if line.startswith("id: "):
                    event_id = line[4:]
                elif line.startswith("data: "):
                    event = json.loads(line[6:])
                    payloads.append((event_id, event))
                    if event["type"] == "done" or (interrupt and event["type"] == "chunk"):
                        break
            return 200, payloads

    full = asyncio.create_task(stream(request_id, "bench-9871"))
    try:
        first_status, prefix = await stream(request_id, "bench-9871", interrupt=True)
        assert first_status == 200 and prefix[-1][1]["type"] == "chunk" and prefix[-1][0]
        resumed_status, suffix = await stream(request_id, "bench-9871", cursor=prefix[-1][0])
        full_status, answer = await full
        assert resumed_status == full_status == 200 and answer[-1][1]["type"] == "done"
        assert prefix + suffix == answer, "Reconnection changed the event suffix"
    finally:
        if not full.done():
            full.cancel()
            await asyncio.gather(full, return_exceptions=True)

    for _ in range(20):
        status, answer = await stream(limited_id, "bench-9872")
        assert status == 200 and answer[-1][1]["type"] == "done"
    status, payload = await stream(limited_id, "bench-9872")
    assert status == 429 and payload["code"] == "AI_USER_RATE_LIMITED"
    counts = await stage.client.post("/__mock/calls", json=[request_id, limited_id])
    counts.raise_for_status()
    assert counts.json() == [1, 1], "Expected one upstream execution per message"
    print(json.dumps({
        "worker_pids": sorted(workers), "replay_suffix_events": len(suffix),
        "shared_user_limit_status": status, "upstream_calls": counts.json(),
    }), flush=True)
    return 0


async def main(args):
    async with httpx.AsyncClient(
        base_url=args.url, trust_env=False, timeout=httpx.Timeout(35, connect=5),
        limits=httpx.Limits(max_connections=args.max_inflight + 4),
    ) as client:
        stage = Stage(client, args)
        if args.verify_workers:
            return await verify_workers(stage, args.verify_workers)
        return await stage.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18765")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seed", default="kuniv-zero-bench-v1")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--concurrency", type=int)
    mode.add_argument("--rate", type=int)
    mode.add_argument("--verify-workers", type=int, choices=(2,))
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--users", type=int, default=512)
    parser.add_argument("--max-inflight", type=int, default=240)
    parser.add_argument("--faults", action="store_true")
    parser.add_argument("--trace-file")
    args = parser.parse_args()
    origin = urlsplit(args.url)
    if (origin.scheme != "http" or origin.hostname != "127.0.0.1" or not origin.port
            or origin.username is not None or origin.path not in ("", "/")
            or origin.query or origin.fragment):
        parser.error("Use an explicit IPv4 loopback SSH tunnel, never a public URL")
    if min(args.seconds, args.users, args.max_inflight) <= 0 or (args.concurrency or args.rate or args.verify_workers or 0) <= 0:
        parser.error("Positive stage parameters required")
    if args.concurrency and args.concurrency > args.max_inflight:
        parser.error("Concurrency must not exceed --max-inflight")
    raise SystemExit(asyncio.run(main(args)))
