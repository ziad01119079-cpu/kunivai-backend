"""Offline checks for the consolidated chat candidate."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import academic_calendar as cal, chat_service as chat
from app.services.model_policy import effective_max_output_tokens, model_options
from app.services.query_router import QueryIntent, classify_query
from app.services.response_policy import response_policy_for
from app.services.stream_citations import StreamCitations
from app.services.verified_answers import CAMPUS_TRANSPORT, HOME_TRANSPORT, STUDENT_PORTAL
from eval.run_rag_sprint2 import GENERAL, LAW, PAGES


CASES = {
    row["id"]: row for row in json.loads(
        (Path(__file__).resolve().parents[1] / "eval/adaptive_length_cases.json").read_text(encoding="utf-8")
    )
}
U02 = "أبي أغيّر تخصصي، شنو الضوابط المطلوبة بالضبط؟"
U12 = "هل أقدر أشتغل داخل الجامعة (التشغيل الطلابي)؟ وشنو الفرق بين الإيرادات والمصروفات في المحاسبة بمثال سريع؟"


def event(kind, **kwargs):
    return "data: " + json.dumps({"type": kind, **kwargs}) + "\n\n"


@pytest.fixture
def run_chat(monkeypatch):
    config = SimpleNamespace(
        OPENAI_VECTOR_STORE_ID="fixture", OPENAI_MODEL="gpt-4o", HISTORY_TOKEN_BUDGET=3000,
        OPENAI_ROUTE_MODELS_ENABLED=False, OPENAI_MODEL_DEFAULT="gpt-6-luna",
        OPENAI_MODEL_DETAILED="", OPENAI_MODEL_RAG="", OPENAI_MODEL_RAG_FALLBACK="gpt-5.4-mini",
        OPENAI_REASONING_EFFORT_DEFAULT="none", OPENAI_REASONING_EFFORT_DETAILED="low",
    )
    completed = AsyncMock(return_value=True)
    calls = []
    checked = datetime(2026, 11, 25, 12, tzinfo=cal.KUWAIT_TZ)
    monkeypatch.setattr(cal, "kuwait_today", lambda: checked.date())
    monkeypatch.setattr(chat, "get_config", lambda: config)
    monkeypatch.setattr(chat, "save_user_message", AsyncMock(return_value=True))
    monkeypatch.setattr(chat, "save_assistant_message", completed)
    monkeypatch.setattr(chat, "load_user_schedules", AsyncMock(return_value=[]))
    monkeypatch.setattr(chat.background_tasks, "create_task", lambda coro: coro.close())

    async def lookup(query):
        async def fetch(url):
            return PAGES[url]
        return await cal.lookup_official_calendar(query, fetcher=fetch, checked_at=checked, cache_ttl_seconds=0)

    monkeypatch.setattr(chat, "lookup_official_calendar", lookup)

    def execute(question, *, history=(), attempts=(), models=False):
        config.OPENAI_ROUTE_MODELS_ENABLED = models
        monkeypatch.setattr(chat, "load_recent_messages", AsyncMock(return_value=[
            {"isUser": row["role"] == "user", "content": row["content"]} for row in reversed(history)
        ]))

        async def stream(messages, prompt, tools=None, **options):
            index = len(calls)
            calls.append((messages, prompt, [t["type"] for t in tools], options))
            for value in attempts[index]:
                yield value

        monkeypatch.setattr(chat, "stream_response", stream)

        async def collect():
            return [json.loads(line[6:]) async for line in chat.process_chat("mock", "mock", question)]

        payloads = asyncio.run(collect())
        return payloads, calls, completed

    return execute


def visible(payloads):
    return "".join(row["content"] for row in payloads if row["type"] == "chunk")


@pytest.mark.parametrize(("case", "expected_sources", "expected_text"), [
    ("Q015", {STUDENT_PORTAL}, "ما قدرت أتحقق من رابط مباشر"),
    ("Q076", {HOME_TRANSPORT, CAMPUS_TRANSPORT}, "النقل المنزلي للطالبات"),
    ("Q002", {LAW, cal.LAW_GUIDANCE_URL}, "غير متحقق"),
])
def test_production_chat_audited_direct_cases(run_chat, case, expected_sources, expected_text):
    item = CASES[case]
    events, calls, _ = run_chat(item["question"], history=item["history"])
    assert not calls
    assert expected_text in visible(events)
    assert events[-1]["type"] == "done"
    assert expected_sources <= {source["url"] for source in events[-1]["citations"]}
    if case == "Q002":
        assert "30 يناير" not in visible(events)


def test_mixed_calendar_preserves_citation_after_instructional_continuation(run_chat):
    answer = "سحب مقرر يخص مادة واحدة، والانسحاب الكلي يخص الفصل كله."
    events, calls, _ = run_chat(CASES["N07"]["question"], attempts=[
        [event("chunk", content=answer), event("done", citations=[])]
    ])
    assert visible(events).count(GENERAL) == 1
    assert "24 أكتوبر 2026" in visible(events) and "انقضى" in visible(events)
    assert answer in visible(events)
    assert GENERAL in {source["url"] for source in events[-1]["citations"]}
    assert calls[0][0][-1]["content"] == CASES["N07"]["question"]
    assert "<verified_application_answer>" in calls[0][1]


@pytest.mark.parametrize("question", [U02, U12, "ما هي قواعد تأجيل الامتحان؟", "ما هي عقوبات الغش؟"])
def test_official_policy_routes_and_has_headroom(question):
    assert classify_query(question) is QueryIntent.OFFICIAL_UNIVERSITY
    assert response_policy_for(question).kind != "simple"


@pytest.mark.parametrize("question", ["شنو تخصصي؟", "كم معدلي؟", "متى اختباري بالجدول؟"])
def test_personal_only_requests_keep_their_route(question):
    assert classify_query(question) is QueryIntent.PERSONAL_SCHEDULE


def test_legitimate_web_fallback_keeps_full_mixed_question_and_can_use_opt_in_model(run_chat):
    policy_url = "https://www.ku.edu.kw/ar/student-employment-fixture"
    answer = "راجع شروط التشغيل الطلابي في المصدر. مثال المحاسبة: بيع الكتب إيراد، وشراء الورق مصروف."
    events, calls, _ = run_chat(U12, models=True, attempts=[
        [event("chunk", content="غير مدعوم"), event("done", citations=[])],
        [event("chunk", content=answer), event("done", citations=[{"url": policy_url}])],
    ])
    assert [item[2] for item in calls] == [["file_search"], ["web_search"]]
    assert [item[3]["model"] for item in calls] == ["gpt-6-luna", "gpt-5.4-mini"]
    assert all(item[0][-1]["content"] == U12 for item in calls)
    assert visible(events) == answer
    assert events[-1]["citations"][0]["url"] == policy_url


@pytest.mark.parametrize("terminal", [[], [event("error", code="OUTPUT_LIMIT_REACHED", usage={"output_tokens": 100})]])
def test_incomplete_retrieval_neither_falls_back_nor_publishes_partial_text(run_chat, terminal):
    events, calls, save = run_chat(U02, attempts=[[event("chunk", content="unverified"), *terminal]])
    assert [item[2] for item in calls] == [["file_search"]]
    assert calls[0][3] == {}
    assert not visible(events)
    assert events[-1]["code"] == ("OUTPUT_LIMIT_REACHED" if terminal else "LLM_STREAM_INTERRUPTED")
    assert "usage" not in events[-1]
    save.assert_not_awaited()


def test_model_routing_preserves_detailed_stem_ceiling_and_legacy_parameters():
    policy = response_policy_for("اشرح بالتفصيل حل التكامل خطوة بخطوة مع مثال")
    cfg = SimpleNamespace(
        OPENAI_ROUTE_MODELS_ENABLED=False, OPENAI_MODEL="gpt-4o", OPENAI_MODEL_DEFAULT="gpt-6-luna",
        OPENAI_MODEL_DETAILED="", OPENAI_REASONING_EFFORT_DETAILED="low",
    )
    assert policy.kind == "detailed"
    assert model_options(cfg, policy, rag=False) == {}
    assert effective_max_output_tokens(policy, hosted_tools=False, effort=None) == policy.max_output_tokens == 2200
    cfg.OPENAI_ROUTE_MODELS_ENABLED = True
    assert model_options(cfg, policy, rag=False) == {"model": "gpt-6-luna", "reasoning_effort": "low"}
    assert effective_max_output_tokens(policy, hosted_tools=True, effort="low") > policy.max_output_tokens
    cfg.OPENAI_MODEL_DEFAULT = ""
    assert model_options(cfg, policy, rag=False) == {"model": "gpt-4o", "reasoning_effort": None}


def test_stream_markers_split_at_every_character_preserve_text_and_offsets():
    cleaner = StreamCitations()
    raw = "قبل 【قائمة】 \ue200filecite\ue202turn0file10\ue202turn0file2\ue201 بعدها【1：2†عنوان】 النص【42"
    assert "".join(cleaner.feed(char) for char in raw) + cleaner.feed("", final=True) == "قبل 【قائمة】  بعدها النص【42"
    assert cleaner.visible_offset(raw.index("النص")) == len("قبل 【قائمة】  بعدها ")


def test_detailed_stem_reaches_teacher_with_requested_example_and_retains_final_work(run_chat):
    question = "اشرح بالتفصيل قانون حفظ الطاقة خطوة بخطوة مع مثال لكتلة 2 kg تسقط من ارتفاع 5 m"
    reasoning = "عند إهمال مقاومة الهواء نحسب طاقة الوضع: 2 × 9.8 × 5 = 98 J.\n"
    final = "بالقرب من الأرض تتحول إلى طاقة حركة، لهذا ½ × 2 × v² = 98، والسرعة ≈ 9.9 m/s."
    events, calls, save = run_chat(question, models=True, attempts=[
        [event("chunk", content=reasoning), event("chunk", content=final), event("done", citations=[])]
    ])
    assert calls[0][2] == []
    assert calls[0][3] == {"model": "gpt-6-luna", "reasoning_effort": "low"}
    assert calls[0][0][-1]["content"] == question
    assert "الحسابات وافتراضاتها" in calls[0][1] and "الأمثلة المطلوبة" in calls[0][1]
    assert visible(events) == reasoning + final
    assert events[-1]["type"] == "done"
    save.assert_awaited_once()


def test_followup_length_and_context_keep_previous_answer(run_chat):
    prior = [
        {"role": "user", "content": "اشرح المشتقة"},
        {"role": "assistant", "content": "المشتقة تقيس معدل تغير الدالة."},
    ]
    events, calls, _ = run_chat("اشرح أكتر", history=prior, attempts=[
        [event("chunk", content="مثلًا مشتقة x² عند x = 3 تساوي 6."), event("done", citations=[])]
    ])
    assert calls[0][2] == []
    assert calls[0][0][-2] == prior[-1]
    assert "لا تعد صياغة الإجابة كاملة" in calls[0][1]
    assert events[-1]["type"] == "done"
