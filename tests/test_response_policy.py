from app.services.response_policy import response_policy_for, response_policy_prompt


def test_simple_arabic_date_question_is_bounded_to_a_short_answer():
    policy = response_policy_for("متى فترة التحويل؟")
    assert policy.kind == "simple"
    assert policy.max_output_tokens <= 220
    assert "جملة أو جملتين" in response_policy_prompt("متى فترة التحويل؟")


def test_procedure_comparison_education_and_requested_detail_are_adaptive():
    assert response_policy_for("شلون أسجل بالمقررات؟").kind == "procedure"
    assert response_policy_for("قارن بين التخصصين").kind == "comparison"
    assert response_policy_for("اشرح التكامل").kind == "educational"
    assert response_policy_for("اشرح الموضوع بالتفصيل").kind == "detailed"


def test_only_current_message_controls_length_policy():
    assert response_policy_for("متى الموعد؟").kind == "simple"
