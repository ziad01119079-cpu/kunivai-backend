"""Routing tests for Community Knowledge, Official isolation, Mixed intents, and M1 flag-off parity."""

from __future__ import annotations

from app.services.query_router import (
    QueryIntent,
    classify_query,
    should_use_file_search,
)


def test_official_acceptance_cases_never_route_to_community_t01_to_t04_t11_t13():
    official_cases = (
        "كم وحدة أقدر أسجل؟",  # T01
        "شروط الإنذار GPA كم؟",  # T03
        "إعادة المقرر يحسبون الأعلى أو الثاني؟",  # T04
        "شلون أغيّر تخصصي؟",  # T11
        "احسب المعدل حسب اللائحة",  # T13
    )
    for query in official_cases:
        assert classify_query(query, community_enabled=True) is QueryIntent.OFFICIAL_UNIVERSITY, query
        assert should_use_file_search(query, community_enabled=True) is True, query

    # T02: semester start date routes to official retrieval (never community)
    assert classify_query("متى تبدأ الدراسة في جامعة الكويت؟", community_enabled=True) in {
        QueryIntent.OFFICIAL_UNIVERSITY,
        QueryIntent.TIME_SENSITIVE_UNIVERSITY,
        QueryIntent.UNCERTAIN,
    }
    assert should_use_file_search("متى تبدأ الدراسة في جامعة الكويت؟", community_enabled=True) is True


def test_community_opinion_acceptance_cases_route_to_community_when_enabled():
    community_cases = (
        "شنو رأي الطلاب في دكتور مادة المحاسبة؟",  # T05
        "مين أسوأ دكتور في الجامعة؟",  # T06
        "شنو أفضل مطعم بالجامعة؟",  # T08
        "الرأي في التسجيل هل فيه مشاكل؟",  # T12
        "أعطني المصادر الأصلية لآراء الدكاترة",  # T15
        "بناءً على 3 تعليقات متكررة كم طالب متفق؟",  # T16
    )
    for query in community_cases:
        assert classify_query(query, community_enabled=True) is QueryIntent.COMMUNITY_OPINION, query
        assert should_use_file_search(query, community_enabled=True) is False, query


def test_live_operational_hours_route_to_time_sensitive_when_enabled_t09():
    assert classify_query("هل مطعم X مفتوح الحين؟", community_enabled=True) is QueryIntent.TIME_SENSITIVE_UNIVERSITY
    assert classify_query("Is the campus cafe open now?", community_enabled=True) is QueryIntent.TIME_SENSITIVE_UNIVERSITY


def test_off_campus_restaurant_remains_out_of_scope():
    assert classify_query("Recommend a restaurant for dinner", community_enabled=True) is QueryIntent.OUT_OF_SCOPE


def test_mixed_official_and_community_acceptance_cases_t07_t10_t21():
    mixed_cases = (
        "انصحني بمادة سهل النجاح فيها",  # T07
        "أبغي آراء الطلاب عن النقل وأوقات الباص",  # T10
        "ما شروط السحب والإضافة وشنو رأي الطلاب في دكتور المحاسبة؟",  # T21
    )
    for query in mixed_cases:
        assert classify_query(query, community_enabled=True) is QueryIntent.MIXED_OFFICIAL_COMMUNITY, query
        assert should_use_file_search(query, community_enabled=True) is True, query


def test_private_schedule_and_private_archive_never_route_to_community_t14_t22():
    assert classify_query("هل سجلت جدول محاضراتي؟", community_enabled=True) is QueryIntent.PERSONAL_SCHEDULE
    assert classify_query("أرشيف جدولي الخاص هل آرائي عامة؟", community_enabled=True) is QueryIntent.PERSONAL_SCHEDULE


def test_m1_feature_off_preserves_exact_legacy_baseline_routing():
    # When community_enabled=False (default), campus restaurant hours stays OUT_OF_SCOPE
    # (does not trigger hosted Web Search) and mixed withdrawal stays OFFICIAL_UNIVERSITY.
    assert classify_query("هل مطعم X مفتوح الحين؟", community_enabled=False) is QueryIntent.OUT_OF_SCOPE
    assert (
        classify_query("ما شروط السحب والإضافة وشنو رأي الطلاب في دكتور المحاسبة؟", community_enabled=False)
        is QueryIntent.OFFICIAL_UNIVERSITY
    )
