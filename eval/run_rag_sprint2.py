"""Frozen, zero-paid-call comparison for mixed intent, retrieval and context."""
import argparse
import asyncio
from contextlib import ExitStack
from datetime import date, datetime
import json
from pathlib import Path
import sys
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "rag_sprint2"
REQUESTS = json.loads((FIXTURES / "requests.json").read_text(encoding="utf8"))
GENERAL = "https://do.ku.edu.kw/ar/academic-calendar-0"
LAW = "https://do.ku.edu.kw/ar/lawcalender"
GUIDE = "https://www.ku.edu.kw/ar/new-student-journey"
PORTAL = "https://portal.ku.edu.kw/sis"
STUDENT_FAQ = "https://www.ku.edu.kw/sites/default/files/2025-08/FAQAr.pdf"
REG_GUIDE = "https://www.ku.edu.kw/sites/default/files/2025-08/StudentGuideAR.pdf"
COURSE_RULES = "https://shi.ku.edu.kw/ar/courses-system-list"
LAW_CONTACT = "https://law.ku.edu.kw/ar/students/guidance-and-counseling"
PAGES = {
    GENERAL: (FIXTURES / "ku_general_excerpt.html").read_text(encoding="utf8"),
    LAW: (FIXTURES / "ku_law_excerpt.html").read_text(encoding="utf8"),
}
SCHEDULES = [{"termKey": "synthetic", "courses": [
    {"courseCode": f"MOCK{i:02}", "courseName": "Synthetic Course"} for i in range(40)
]}]
CONFIG = SimpleNamespace(HISTORY_TOKEN_BUDGET=3000, OPENAI_VECTOR_STORE_ID="fixture", OPENAI_MODEL="gpt-4o")
EDUCATIONAL_MOCKS = {
    "mixed-guide": "راجع جدولك، فعّل بريدك الجامعي، وافتح Moodle قبل بدء المحاضرات.",
    "mixed-registration": "ابدأ من نظام معلومات الطالب، وراجع المواد المتاحة ضمن فترة الإضافة قبل تأكيد التسجيل.",
    "club-affiliation": "عادةً يُعتمد النادي من الجامعة وينسّق أنشطته مع مكتب الطلبة.",
    "site-affiliation": "افتح فهرس الجامعة الرسمي وتحقق أن الجهة تربط بالموقع.",
    "gemini-programming": "Start with a Python function and ask Gemini to explain each line.",
    "gemini-topic-switch": "ابدأ بتعريف دالة في بايثون باستخدام def، ثم اختبرها بمدخل بسيط.",
    "gpa-example": "مثال: 3 نقاط × 3 وحدات، و4 × وحدتين، تعطي 17 ÷ 5 = 3.4.",
    "warning-general": "عمومًا تربط الجامعات الإنذار بالمعدل وشروط الكلية، وقد تختلف آلية رفعه.",
    "warning-provided": "ذكرت معدل 1.8 و36 وحدة و12 وحدة جديدة. أقدر أحسب سيناريو مشروطًا بسلم الدرجات، أما قرار رفع الإنذار فيعتمد على لائحة كليتك.",
    "exam-explanation": "الميد يقيس جزءًا من المنهج أثناء الفصل، والفاينل يكون في نهايته.",
    "schedule-followup": "وزّع مراجعة Synthetic Course على ثلاث جلسات.",
    "hold-file": "لا يظهر الملف نفسه في البيانات المتاحة لي. أرسل نص الصفحات المطلوبة لألخّصها.",
    "hold-pronoun": "المشتقة تقيس مقدار تغيّر الناتج عندما يتغير المدخل قليلًا.",
    "hold-mixed-date": "سحب مقرر يخص مادة واحدة، والانسحاب الكلي يخص الفصل كله.",
}
NEEDED_PREFIX = {"guide": GUIDE, "registration": PORTAL, "formula": COURSE_RULES, "mixed_date": GENERAL}
PROVIDER_SOURCES = {"mixed-registration": REG_GUIDE, "warning-provided": STUDENT_FAQ}


async def evaluate(case):
    from app.history_builder import _estimate_tokens
    from app.services import academic_calendar as cal
    from app.services import chat_service as chat

    name, question, history, with_schedule, kind = case
    calls = []
    fetched = []
    checked = datetime(2026, 11, 25, 12, tzinfo=cal.KUWAIT_TZ)
    prior = [{"isUser": True, "content": msg} for msg in history]

    async def fetch(url):
        fetched.append(url)
        return PAGES[url]

    async def lookup(query):
        return await cal.lookup_official_calendar(query, fetcher=fetch, checked_at=checked, cache_ttl_seconds=0)

    async def stream(messages, prompt, tools=None):
        calls.append({
            "tools": [x["type"] for x in tools or []],
            "input_tokens_est": _estimate_tokens(prompt) + sum(_estimate_tokens(m["content"]) for m in messages),
            "question_exact": messages[-1]["content"] == question,
            "history_present": all(msg in [m["content"] for m in messages[:-1]] for msg in history),
            "schedule_present": "Synthetic Course" in prompt,
            "trusted_context": "<verified_application_answer>" in prompt,
            "reuses_inputs_instruction": "لا تطلبها مرة أخرى" in prompt or "Reuse inputs supplied" in prompt,
        })
        content = EDUCATIONAL_MOCKS[name]
        # The SDK/provider is never used. An educational reply is scripted.
        # For protected procedures use a known primary source, as if retrieved.
        url = PROVIDER_SOURCES.get(name, GUIDE)
        yield "data: " + json.dumps({"type": "chunk", "content": content}) + "\n\n"
        yield "data: " + json.dumps({"type": "done", "citations": [
            {"type": "url_citation", "url": url, "title": "Fixture official evidence"}
        ] if tools else []}) + "\n\n"

    def discard(coro):
        coro.close()

    cal.clear_calendar_content_cache()
    t0 = perf_counter()
    ttft_ms = None
    with ExitStack() as stack:
        stack.enter_context(patch("app.responses_api.get_openai_client", side_effect=AssertionError("no network provider")))
        stack.enter_context(patch.object(cal, "kuwait_today", return_value=checked.date()))
        stack.enter_context(patch.object(chat.background_tasks, "create_task", side_effect=discard))
        for key, value in {
            "get_config": lambda: CONFIG,
            "load_recent_messages": AsyncMock(return_value=prior),
            "load_user_schedules": AsyncMock(return_value=SCHEDULES if with_schedule else []),
            "save_user_message": AsyncMock(return_value=True),
            "save_assistant_message": AsyncMock(return_value=True),
            "stream_response": stream,
            "lookup_official_calendar": lookup,
        }.items():
            stack.enter_context(patch.object(chat, key, value))
        events = []
        async for item in chat.process_chat("synthetic", "synthetic", question):
            event = json.loads(item[6:])
            events.append(event)
            if event["type"] == "chunk" and ttft_ms is None:
                ttft_ms = (perf_counter() - t0) * 1000

    text = "".join(e["content"] for e in events if e["type"] == "chunk")
    citations = [c["url"] for e in events if e["type"] == "done" for c in e.get("citations", []) if "url" in c]
    checks = {"terminal": events[-1]["type"] == "done"}
    if name in EDUCATIONAL_MOCKS:
        checks["remaining_subtask"] = EDUCATIONAL_MOCKS[name] in text
        checks["original_question_and_history"] = bool(calls and all(c["question_exact"] and c["history_present"] for c in calls))
        if kind in NEEDED_PREFIX:
            url = NEEDED_PREFIX[kind]
            checks["verified_prefix"] = url in citations and f"]({url})" in text
            checks["trusted_context"] = bool(calls and calls[0]["trusted_context"])
        if kind == "mixed_date":
            checks["verified_deadline"] = "24 أكتوبر 2026" in text and "انقضى" in text
        if kind in {"schedule", "file", "academic"}:
            checks["no_public_search"] = bool(calls and not any(c["tools"] for c in calls))
        if kind == "schedule":
            checks["structured_schedule"] = bool(calls and calls[0]["schedule_present"])
        if kind == "standing":
            checks["warn_inputs_reused"] = bool(calls and calls[-1]["reuses_inputs_instruction"] and "كم معدلك" not in text)
    elif kind == "direct_guide":
        checks["verified_link_only"] = not calls and GUIDE in citations and f"]({GUIDE})" in text
    elif kind == "arithmetic":
        checks["arithmetic"] = not calls and "2 + 3 = 5" in text
    elif kind == "law_uncertain":
        checks["law_source"] = LAW in citations
        checks["useful_abstention"] = not calls and "غير متحقق" in text and LAW_CONTACT in text and "30 يناير" not in text
    return dict(id=name, checks=checks, passed=all(checks.values()), provider_attempts=calls, checked_urls=fetched,
                answer=text, citations=citations, ttft_ms=ttft_ms, total_ms=(perf_counter() - t0) * 1000)


async def calendar_cases():
    from app.services import academic_calendar as cal
    checked = datetime(2026, 11, 25, 12, tzinfo=cal.KUWAIT_TZ)

    async def fetch(url):
        return PAGES[url]

    results = []
    for year, expected in (("2026/2027", ["2027-06-01", "2027-06-03"]),
                           ("2027/2028", ["2028-05-30", "2028-06-01"])):
        query = cal.CalendarQuery(cal.CalendarEvent.TRANSFER, year, cal.Semester.SECOND)
        result = cal.verify_official_calendar_html(query, PAGES[GENERAL], cal.OFFICIAL_SOURCES[cal.CollegeScope.GENERAL], checked_at=checked)
        dates = [str(d) for row in result.records for d in (row.start_date, row.end_date)]
        results.append(dict(id="spring-transfer-" + year, passed=result.status == "verified" and dates == expected,
                            status=result.status, dates=dates, expected=expected))
    query = cal.resolve_calendar_query("متى آخر يوم سحب مواد في التقويم العام؟", [], today=date(2027, 1, 15))
    result = await cal.lookup_official_calendar(query, fetcher=fetch, checked_at=checked, cache_ttl_seconds=0)
    dates = [str(r.end_date) for r in result.general.records]
    results.append(dict(id="january-boundary", passed=dates == ["2026-10-24"], dates=dates))
    query = cal.CalendarQuery(cal.CalendarEvent.FULL_SEMESTER_WITHDRAWAL, "2026/2027", cal.Semester.FIRST)
    result = cal.verify_official_calendar_html(query, PAGES[GENERAL], cal.OFFICIAL_SOURCES[cal.CollegeScope.GENERAL], checked_at=checked)
    results.append(dict(id="published-year-conflict", passed=result.status in {"missing", "conflict"} and not result.records,
                        status=result.status))
    return results


async def run():
    # Prime local tokenizer tables once on both revisions before timing.
    from app.history_builder import _estimate_tokens
    _estimate_tokens("warm local tokenizer")
    return {"requests": [await evaluate(case) for case in REQUESTS], "calendar": await calendar_cases()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, default=ROOT)
    args = parser.parse_args()
    sys.path.insert(0, str(args.backend.resolve()))
    print(json.dumps(asyncio.run(run()), ensure_ascii=False))


if __name__ == "__main__":
    main()
