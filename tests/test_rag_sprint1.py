import asyncio
import json
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest

from app import responses_api
from app.logger import JSONFormatter, chat_id_var, request_id_var, uid_var
from app.services.academic_calendar import CollegeScope, OfficialCalendarSource, resolve_calendar_query, verify_official_calendar_html
from app.services.grounding import applicable_citations, official_ku_url
from app.services.provider_usage import ProviderAttempt, UsageTrace, current_usage, estimate_cost
from eval.run_rag_sprint1 import CHECKED, GENERAL, LAW, LAW_HTML, run


@pytest.mark.parametrize("split", ["development", "challenge"])
def test_frozen_eval(split):
    results = asyncio.run(run(split))
    assert all(r["passed"] for r in results), [(r["id"], r["checks"]) for r in results if not r["passed"]]


@pytest.mark.parametrize("url", ["https://sar.ku.edu/refunds", "https://ku.edu.kw.fake.test/",
                                 "https://ku.edu.kw@other.test", "http://ku.edu.kw", "https://ku.edu.kw:bad"])
def test_official_origin_uses_https_and_exact_dns_suffix(url):
    assert not official_ku_url(url)
    assert applicable_citations([{"url": url}], require_official=True) == []


def test_calendar_does_not_steal_neighboring_vacation_dates():
    q = resolve_calendar_query("When are final exams at the College of Law?", [], today=CHECKED.date())
    src = OfficialCalendarSource(CollegeScope.LAW, LAW, "الحقوق", "Law")
    assert verify_official_calendar_html(q, LAW_HTML, src, checked_at=CHECKED).status == "missing"


def test_calendar_requires_first_year_evidence_on_the_event_row():
    query = resolve_calendar_query("متى اختبارات الفاينل سنة أولى في كلية الحقوق؟", [], today=CHECKED.date())
    src = OfficialCalendarSource(CollegeScope.LAW, LAW, "الحقوق", "Law")
    html = LAW_HTML.replace(
        "<td>Unified Final Examination Period</td>",
        "<td>20/12/2026</td><td>24/12/2026</td><td>Unified Final Examination Period</td>",
    )
    assert verify_official_calendar_html(query, html, src, checked_at=CHECKED).status == "missing"
    eligible = html.replace("Unified Final Examination Period", "Unified Final Examination Period for first-year students")
    result = verify_official_calendar_html(query, eligible, src, checked_at=CHECKED)
    assert result.status == "verified"
    assert result.records[0].end_date.isoformat() == "2026-12-24"


def test_calendar_refuses_non_ku_source_even_with_matching_rows():
    q = resolve_calendar_query("متى وقف القيد؟", [], today=CHECKED.date())
    src = OfficialCalendarSource(CollegeScope.GENERAL, "https://sar.ku.edu", "مشابه", "Other university")
    assert verify_official_calendar_html(q, "<h2>Fall Semester 2026/2027</h2><p>Enrollment Suspension 22/10/2026</p>", src).status == "unsupported_scope"


class FakeStream:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        async def stream():
            for event in self.events:
                yield event
        return stream()

    async def __aexit__(self, *_args):
        return False


def test_usage_includes_retry_and_tool_counts_without_text(caplog):
    usage = NS(input_tokens=10_000, output_tokens=1000, total_tokens=11_000,
               input_tokens_details=NS(cached_tokens=4000))
    fs = NS(type="file_search_call", id="private-item-1", status="completed", results=[NS(text="private retrieved text")])
    ws = NS(type="web_search_call", id="private-item-2", status="completed")
    response = NS(model="gpt-4o-2024-08-06", output=[fs, ws], usage=usage)
    failed_response = NS(model="gpt-4o-2024-08-06", output=[], error=NS(code="server_error"), usage=None)
    upstream = [
        FakeStream([NS(type="response.failed", response=failed_response)]),
        FakeStream([NS(type="response.output_item.done", item=fs), NS(type="response.completed", response=response)]),
    ]
    mock_client = NS(responses=NS(stream=lambda **_: upstream.pop(0)))
    trace = UsageTrace(route="official_university", fallback_reason="missing_file_citations")

    async def evaluate():
        token = current_usage.set(trace)
        try:
            return [item async for item in responses_api.stream_response(
                [{"role": "user", "content": "private question"}], "private system prompt", tools=[{"type": "web_search"}]
            )]
        finally:
            current_usage.reset(token)

    with caplog.at_level(logging.INFO, logger="app.services.provider_usage"), patch.object(
        responses_api, "get_openai_client", return_value=mock_client
    ), patch.object(responses_api, "get_config", return_value=NS(OPENAI_MODEL="gpt-4o")):
        events = asyncio.run(evaluate())
    assert json.loads(events[-1][6:])["type"] == "done"
    first, second = trace.attempts
    assert first["estimated_cost_usd"] is None
    assert second["actual_model"] == "gpt-4o-2024-08-06"
    assert second["retry_reason"] == "server_error"
    assert second["fallback_reason"] == "missing_file_citations"
    assert second["file_search_calls_observed"] == second["web_search_calls_observed"] == 1
    assert second["estimated_cost_usd"] == pytest.approx(0.0425)
    tokens = [var.set("private session field") for var in (uid_var, chat_id_var, request_id_var)]
    try:
        for record in caplog.records:
            if not getattr(record, "cost_telemetry", False):
                continue
            contents = JSONFormatter().format(record)
            assert "private" not in contents
    finally:
        for var, token in zip((uid_var, chat_id_var, request_id_var), tokens):
            var.reset(token)


def test_background_usage_joins_request_totals(caplog):
    from app.services import missed_query_service as missed
    complete = NS(model="gpt-4o-mini-2024-07-18", usage=NS(prompt_tokens=120, completion_tokens=9),
                  choices=[NS(message=NS(content='{"category":"academic","reason":"Ambiguous question"}'))])
    provider = NS(chat=NS(completions=NS(create=AsyncMock(return_value=complete))))
    trace = UsageTrace()
    token = current_usage.set(trace)
    try:
        with patch.object(missed, "get_openai_client", return_value=provider), patch.object(
            missed, "upsert_missed_query", new_callable=AsyncMock
        ), caplog.at_level(logging.INFO, logger="app.services.provider_usage"):
            asyncio.run(missed.analyze_and_log_missed_query("private question", "private chat", True, "gpt-4o"))
    finally:
        current_usage.reset(token)
    assert trace.attempts[0]["operation"] == "missed_query_classification"
    assert trace.attempts[0]["estimated_cost_usd"] == pytest.approx(0.0000234)
    assert any(record.operational_metadata.get("phase") == "including_background" for record in caplog.records)


def test_missing_actual_model_is_not_priced_as_gpt_4o_mini():
    tokens = {"input_tokens": 1000, "output_tokens": 100, "cached_tokens": 0}
    assert estimate_cost(None, tokens, 0, 0) is None
    assert estimate_cost("gpt-4o", tokens, 0, 0) == pytest.approx(0.0035)
    assert estimate_cost("gpt-4o-mini", tokens, 0, 0) == pytest.approx(0.00021)
