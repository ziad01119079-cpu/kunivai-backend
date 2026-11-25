"""Pair baseline and candidate GPT-4o answers on frozen anonymized fixtures.

Default mode prepares both prompts without a network call. Live mode requires a
total budget, runs without search and retries, and uses a fixed OpenAI endpoint.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
CASES = HERE / "adaptive_length_cases.json"
INPUT_RATE = Decimal("0.00000250")
CACHED_RATE = Decimal("0.00000125")
OUTPUT_RATE = Decimal("0.00001000")
MAX_APPROVED = Decimal("3")
MODEL = "gpt-4o"


def prepared(backend):
    sys.path.insert(0, str(backend.resolve()))
    from app import prompts
    from app.services.response_policy import response_policy_for, response_policy_prompt

    prompts._kuwait_date = lambda: "2026-11-25"
    supports_history = "previous_answer" in inspect.signature(response_policy_for).parameters
    rows = []
    for case in json.loads(CASES.read_text(encoding="utf8")):
        question, history = case["question"], case["history"]
        kw = {"previous_answer": any(m["role"] == "assistant" for m in history)} if supports_history else {}
        policy = response_policy_for(question, **kw)
        guidance = response_policy_prompt(question, **kw)
        context = (
            "\n\nEvaluation setup: no tools or current external evidence are available. The following "
            "frozen summaries represent the applicable retrieved evidence. Use only the supplied evidence "
            "for KU-specific claims. Cite the exact linked source for any such supported claim. "
            "When evidence is missing, state it briefly without inventing a date, process or link.\n"
            + json.dumps(case["evidence"], ensure_ascii=False)
        )
        system = prompts.get_system_prompt(academic_calendar_context=guidance) + context
        rows.append({
            "id": case["id"], "kind": policy.kind, "cap": policy.max_output_tokens, "guidance": guidance,
            "input": [{"role": "system", "content": system}, *history, {"role": "user", "content": question}],
        })
    return rows


def load_prepared(backend):
    data = subprocess.check_output(
        [sys.executable, str(Path(__file__).resolve()), "--prepare", str(backend)],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return json.loads(data)


def reserve(row):
    size = sum(len(item["content"].encode("utf8")) for item in row["input"]) + 256
    return size * INPUT_RATE + row["cap"] * OUTPUT_RATE


def run_live(pairs, output, ceiling, key_env_file, prior_spend):
    from dotenv import dotenv_values
    from openai import OpenAI, APIError

    key = os.environ.get("OPENAI_API_KEY")
    if not key and key_env_file:
        key = dotenv_values(key_env_file, interpolate=False).get("OPENAI_API_KEY")
    if not key or not key.startswith("sk-"):
        raise SystemExit("No usable configured API credential. No requests were sent.")

    # All requests are reserved before opening the API client. No retry or
    # hosted tool can add calls or fees to this run.
    planned = sum((reserve(row) for row in pairs), Decimal(0))
    if not ceiling.is_finite() or ceiling <= 0 or ceiling > MAX_APPROVED or prior_spend + planned > ceiling:
        raise SystemExit("Requested pairs exceed the approved dollar ceiling. No requests were sent.")

    client = OpenAI(api_key=key, base_url="https://api.openai.com/v1", max_retries=0, timeout=45.0)

    def generate(row):
        try:
            res = client.responses.create(
                model=MODEL, input=row["input"], max_output_tokens=row["cap"],
                temperature=0, tools=[], store=False,
            )
        except APIError as exc:
            return {"id": row["id"], "variant": row["variant"],
                    "status": "api_error", "error_type": type(exc).__name__,
                    "status_code": getattr(exc, "status_code", None)}
        usage = res.usage
        cached = usage.input_tokens_details.cached_tokens if usage and usage.input_tokens_details else 0
        cost = (
            (usage.input_tokens - cached) * INPUT_RATE + cached * CACHED_RATE
            + usage.output_tokens * OUTPUT_RATE
        ) if usage else None
        return {
            "id": row["id"], "variant": row["variant"], "kind": row["kind"], "cap": row["cap"],
            "actual_model": res.model, "status": res.status,
            "incomplete_reason": res.incomplete_details.reason if res.incomplete_details else None,
            "input_tokens": usage.input_tokens if usage else None,
            "cached_tokens": cached if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
            "estimated_usd": str(cost) if cost is not None else None,
            "answer": res.output_text,
        }

    results = []
    # Create the file exclusively to prevent an accidental rerun or overwrite.
    with output.open("x", encoding="utf8") as f, client, ThreadPoolExecutor(max_workers=4) as pool:
        f.write(json.dumps({"model": MODEL, "planned_calls": len(pairs), "approved_usd": str(ceiling),
                            "prior_estimated_usd": str(prior_spend),
                            "reserved_upper_estimate_usd": str(planned)}) + "\n")
        for start in range(0, len(pairs), 4):
            batch = list(pool.map(generate, pairs[start:start + 4]))
            for row in batch:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            results.extend(batch)
            if any(row["status"] == "api_error" or not row.get("estimated_usd") for row in batch):
                break
    print(json.dumps({"recorded_attempts": len(results), "planned_calls": len(pairs),
                      "api_errors": sum(r["status"] == "api_error" for r in results),
                      "estimated_usd": str(sum((Decimal(r["estimated_usd"]) for r in results if r.get("estimated_usd")), Decimal(0))),
                      "output": str(output)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--approved-usd", type=Decimal)
    parser.add_argument("--key-env-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reuse-baseline-from", type=Path)
    parser.add_argument("--include-spend-from", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.prepare:
        print(json.dumps(prepared(args.prepare), ensure_ascii=False))
        return
    if not args.baseline:
        parser.error("--baseline is required")
    old = load_prepared(args.baseline)
    new = load_prepared(HERE.parent)
    pairs = []
    for i, (a, b) in enumerate(zip(old, new, strict=True)):
        assert a["id"] == b["id"]
        examples = (dict(a, variant="baseline"), dict(b, variant="candidate"))
        pairs.extend(examples if i % 2 == 0 else reversed(examples))
    if args.live:
        if args.approved_usd is None or args.output is None:
            parser.error("--live requires --approved-usd and --output")
        prior_spend = Decimal(0)
        if args.reuse_baseline_from:
            # Reuse completed baseline answers, but charge both sides of the
            # recorded earlier run against the same approved total.
            prior = [json.loads(line) for line in args.reuse_baseline_from.read_text(encoding="utf8").splitlines()[1:]]
            expected = {(row["id"], row["variant"]) for row in pairs}
            if (
                len(prior) != len(expected) or {(row["id"], row["variant"]) for row in prior} != expected
                or any(row["status"] not in {"completed", "incomplete"} or row.get("estimated_usd") is None
                       or not row.get("actual_model", "").startswith("gpt-4o-") for row in prior)
            ):
                parser.error("earlier run must contain both metered attempts for every frozen fixture")
            prior_spend = sum(Decimal(row["estimated_usd"]) for row in prior)
            pairs = [row for row in pairs if row["variant"] == "candidate"]
        charged_paths = {args.reuse_baseline_from.resolve()} if args.reuse_baseline_from else set()
        for file in args.include_spend_from:
            if file.resolve() in charged_paths:
                parser.error("a prior usage ledger was supplied twice")
            charged_paths.add(file.resolve())
            data = [json.loads(line) for line in file.read_text(encoding="utf8").splitlines()]
            if len(data) != data[0]["planned_calls"] + 1 or any(row.get("estimated_usd") is None for row in data[1:]):
                parser.error("prior usage ledger is incomplete or unmetered; no requests sent")
            prior_spend += sum(Decimal(row["estimated_usd"]) for row in data[1:])
        run_live(pairs, args.output, args.approved_usd, args.key_env_file, prior_spend)
        return
    print(json.dumps({
        "planned_calls": len(pairs), "reserved_upper_estimate_usd": str(sum(map(reserve, pairs))),
        "policies": [{k: row[k] for k in ("id", "variant", "kind", "cap")} for row in pairs],
    }))


if __name__ == "__main__":
    main()
