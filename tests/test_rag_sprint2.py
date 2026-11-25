import asyncio

import pytest

from eval.run_rag_sprint2 import REQUESTS, calendar_cases, evaluate


@pytest.mark.parametrize("case", REQUESTS, ids=[case[0] for case in REQUESTS])
def test_frozen_student_requests(case):
    result = asyncio.run(evaluate(case))
    assert result["passed"], result


def test_current_ku_calendar_html():
    results = asyncio.run(calendar_cases())
    assert all(result["passed"] for result in results), results



def test_mixed_registration_preserves_only_verified_part_if_provider_cites_wrong_institution(monkeypatch):
    from eval.run_rag_sprint2 import PORTAL, PROVIDER_SOURCES, EDUCATIONAL_MOCKS
    case = next(case for case in REQUESTS if case[0] == "mixed-registration")
    monkeypatch.setitem(PROVIDER_SOURCES, case[0], "https://sar.ku.edu/refunds")
    result = asyncio.run(evaluate(case))
    assert "لن أخمّن" in result["answer"]
    assert PORTAL in result["answer"]
    assert EDUCATIONAL_MOCKS[case[0]] not in result["answer"]
    assert result["citations"] == [PORTAL]
    assert [call["tools"] for call in result["provider_attempts"]] == [["file_search"], ["web_search"]]


def test_calendar_conflicting_pair_is_not_reordered_or_covered_with_neighbor_dates():
    from app.services import academic_calendar as cal
    query = cal.CalendarQuery(cal.CalendarEvent.TRANSFER, "2027/2028", cal.Semester.SECOND)
    document = """
    <h2>الفصل الدراسي الثاني 2028/2027</h2>
    <div class="sui-calendar"><h2>فترة التحويل</h2>
    <div class="from_date">1/6/2028</div><div class="to_date">30/5/2028</div></div>
    <div class="sui-calendar"><h2>إجازة أعضاء هيئة التدريس والطلبة</h2>
    <div class="from_date">28/5/2028</div><div class="to_date">9/9/2028</div></div>
    """
    result = cal.verify_official_calendar_html(query, document, cal.OFFICIAL_SOURCES[cal.CollegeScope.GENERAL])
    assert result.status == "conflict"
    assert not result.records



def test_grade_only_followup_does_not_ask_again_when_inputs_appeared_earlier():
    from app.services.verified_answers import known_answer
    history = [{"isUser": False, "content": "ما درجاتك المتوقعة؟"},
               {"isUser": True, "content": "أقصد الإنذار الحالي"},
               {"isUser": True, "content": "معدلي 1.80 وفي حسابه 36 وحدة"}]
    assert known_answer("لو جبت A وB- وD هل يروح الإنذار الأكاديمي؟", history) is None
