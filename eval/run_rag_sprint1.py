"""Run frozen RAG Sprint 1 cases against any local backend checkout, entirely offline."""
import argparse
import asyncio
from contextlib import ExitStack
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

EVAL = Path(__file__).resolve().parent
GENERAL = "https://do.ku.edu.kw/ar/academic-calendar-0"
LAW = "https://do.ku.edu.kw/ar/lawcalender"
CHECKED = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)

GENERAL_HTML = f"""
<a href="{LAW}">تقويم كلية الحقوق</a>
<h2>الفصل الدراسي الأول 2026/2027</h2>
<div>آخر يوم لوقف القيد</div><div>22/10/2026</div>
<div>آخر يوم للانسحاب من المقررات</div><div>24/10/2026</div>
<div>فترة الامتحانات النهائية</div><div>23/12/2026</div><div>4/1/2027</div>
<div>إجازة الطلاب والهيئة</div><div>10/1/2027</div><div>30/1/2027</div>
<h2>الفصل الدراسي الأول 2027/2028</h2>
<div>فترة التحويل</div><div>11/1/2028</div><div>13/1/2028</div>
"""
LAW_HTML = """
<h2>2026/2027</h2><h3>Fall Semester</h3>
<table>
<tr><td>22/10/2026</td><td>Last Day for Course Withdrawal</td></tr>
<tr><td>Unified Final Examination Period</td></tr>
<tr><td>10/01/2027</td><td>30/01/2027</td><td>Faculty and Students’ Vacation</td></tr>
</table>
"""
CONFIG = SimpleNamespace(HISTORY_TOKEN_BUDGET=3000, OPENAI_VECTOR_STORE_ID="fixture", OPENAI_MODEL="gpt-4o")
SCHEDULES = [{"termKey": "fixture", "term": {"academicYear":"2026/2027"}, "courses": [
    {"courseCode": f"T{i:02}", "courseName": "Synthetic Course", "section": str(i)}
    for i in range(40)
]}]


async def evaluate(case):
    from app.history_builder import _estimate_tokens
    from app.services import academic_calendar as cal
    from app.services import chat_service as app

    async def fetch(url):
        return {GENERAL: GENERAL_HTML, LAW: LAW_HTML}[url]
    orig_lookup = cal.lookup_official_calendar
    async def lookup(query):
        return await orig_lookup(query, fetcher=fetch, checked_at=CHECKED, cache_ttl_seconds=0)

    attempts = []
    async def stream(messages, prompt, tools=None):
        schedule = re.search(r"<student_schedule_data>\s*(.*?)\s*</student_schedule_data>", prompt, re.S)
        context = re.search(r"<user_provided_context>\s*(.*?)\s*</user_provided_context>", prompt, re.S)
        system_text = prompt
        if schedule:
            system_text = system_text.replace(schedule.group(0), "", 1)
        if context:
            system_text = system_text.replace(context.group(0), "", 1)
        attempts.append({
            "tools": [tool["type"] for tool in tools or []],
            "total_text_tokens": _estimate_tokens(prompt) + sum(_estimate_tokens(m["content"]) for m in messages),
            "system_text_tokens": _estimate_tokens(system_text),
            "schedule_text_tokens": _estimate_tokens(schedule.group(1)) if schedule else 0,
            "history_text_tokens": sum(_estimate_tokens(m["content"]) for m in messages[:-1]),
            "question_text_tokens": _estimate_tokens(messages[-1]["content"]),
        })
        text = case["mock_answer"] or "تاريخ غير مدعوم."
        urls = case.get("mock_urls", [])
        yield "data: " + json.dumps({"type": "chunk", "content": text}) + "\n\n"
        yield "data: " + json.dumps({"type": "done", "citations": [{"type":"url_citation","url":url} for url in urls]}) + "\n\n"

    def discard(coro):
        coro.close()

    history = [{"isUser": True, "content": text} for text in case.get("prior", [])]
    schedules = SCHEDULES if case["id"] in case.get("with_schedule", case["id"] in {"H05", "H06"}) else []
    cal.clear_calendar_content_cache()
    with ExitStack() as stack:
        for name, mock in {
            "get_config": lambda: CONFIG,
            "load_recent_messages": AsyncMock(return_value=history),
            "load_user_schedules": AsyncMock(return_value=schedules),
            "save_user_message": AsyncMock(return_value=True),
            "save_assistant_message": AsyncMock(return_value=True),
            "stream_response": stream,
            "lookup_official_calendar": lookup,
        }.items():
            stack.enter_context(patch.object(app, name, mock))
        stack.enter_context(patch.object(app.background_tasks, "create_task", side_effect=discard))
        stack.enter_context(patch.object(cal, "kuwait_today", return_value=date(2026, 10, 24)))
        # Any accidental real path fails the offline harness.
        stack.enter_context(patch("app.responses_api.get_openai_client", side_effect=AssertionError("offline")))
        events = [json.loads(item[6:]) async for item in app.process_chat("fixture", "fixture", case["question"])]

    text = "".join(event["content"] for event in events if event.get("type") == "chunk")
    citations = [c.get("url", "") for e in events if e.get("type") == "done" for c in e["citations"]]
    present = [value in text for value in case["expect"]]
    forbidden = any(value in text for value in case.get("ban", []))
    applicability = not forbidden and all(url in citations for url in case["required_urls"])
    if case.get("require_no_citations"):
        applicability &= not citations
    citation_ok = all(url in citations for url in case["required_urls"])
    if case.get("require_no_citations"):
        citation_ok &= not citations
    if citations:
        citation_ok &= all(re.match(r"https://(?:[a-z0-9-]+\.)*ku\.edu\.kw/", url) is not None for url in citations)
    if case["id"] in {"Q099", "Q100"}:
        citation_ok &= all(f"]({url})" in text for url in case["required_urls"])
    routed = len(attempts) <= case.get("max_attempts", 999)
    if "required_tools" in case:
        routed &= bool(attempts) and all(attempt["tools"] == case["required_tools"] for attempt in attempts)
    checks = {
        "factual_accuracy": all(present) and not forbidden if case["verdict"] == "verified" else None,
        "completeness": all(present),
        "source_applicability": applicability,
        "citations": citation_ok,
        "response_length": 0 < len(text.split()) <= 95,
        "usefulness": bool(present and present[0]) and not forbidden,
        "routing": routed,
        "terminal_done": bool(events and events[-1]["type"] == "done"),
    }
    return {
        "id": case["id"], "expected_behavior": case["verdict"], "checks": checks,
        "passed": all(value for value in checks.values() if value is not None),
        "attempts": attempts, "answer": text, "citations": citations,
    }


async def run(split):
    cases = json.loads((EVAL / f"rag_sprint1_{split}.json").read_text(encoding="utf8"))
    return [await evaluate(case) for case in cases]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, default=EVAL.parent)
    parser.add_argument("--split", choices=("development", "challenge", "holdout"), required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.backend.resolve()))
    print(json.dumps(asyncio.run(run(args.split)), ensure_ascii=False))


if __name__ == "__main__":
    main()
