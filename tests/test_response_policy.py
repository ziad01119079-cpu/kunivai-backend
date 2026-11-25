import pytest

from app.services.response_policy import has_previous_answer, response_policy_for, response_policy_prompt


def test_factual_and_calendar_answers_use_different_guidance_with_headroom():
    simple = response_policy_for("What is a prime number?")
    academic = response_policy_for("متى فترة التحويل؟")
    assert simple.kind == "simple"
    assert academic.kind == "academic"
    assert "جملة أو جملتين" in simple.instruction
    assert "نطاقه الفعلي" in academic.instruction
    assert "رابط المصدر الرسمي" in academic.instruction
    assert academic.max_output_tokens > simple.max_output_tokens > 220


@pytest.mark.parametrize("message,kind", [
    ("شلون أسجل بالمقررات؟", "procedure"),
    ("ارسل رابط الدعم التقني", "link"),
    ("Send me the official link for technical support and explain how to use it", "mixed"),
    ("قارن بين التخصصين", "comparison"),
    ("اشرح كيف أحسب التكامل", "educational"),
    ("وضح لي التكامل", "educational"),
    ("مثال على المشتقة", "educational"),
    ("Help me study integration", "educational"),
    ("Give an example of a prime", "educational"),
    ("اشرح الموضوع بالتفصيل", "detailed"),
    ("Explain limits step by step", "detailed"),
    ("متى التحويل، واشرح شروطه", "mixed"),
    ("What is a derivative and show a worked example", "mixed"),
    ("اشرح أكتر", "educational"),
])
def test_requested_depth_and_mixed_intent(message, kind):
    assert response_policy_for(message).kind == kind


@pytest.mark.parametrize("message", ["اشرح أكتر", "وضّح أكثر", "مثال ثاني", "Could you explain that further?"])
def test_explicit_followup_uses_prior_answer(message):
    assert response_policy_for(message, previous_answer=True).kind == "follow_up"
    assert "لا تعد صياغة الإجابة كاملة" in response_policy_prompt(message, previous_answer=True)


def test_history_does_not_inflate_unrelated_topic():
    history = [{"role": "user", "content": "اكتب 1000 كلمة"}, {"role": "assistant", "content": "شرح سابق"}]
    assert has_previous_answer(history)
    assert response_policy_for("متى الموعد؟", previous_answer=True).kind == "simple"
    assert response_policy_for("اعطني مثال عن الكسور", previous_answer=True).kind == "educational"
    assert response_policy_for("Give me an example of a prime.", previous_answer=True).kind == "educational"


def test_explicit_brevity_keeps_example_and_citations():
    guidance = response_policy_prompt("اشرح التكامل بمثال باختصار")
    assert "طلب الطالب الاختصار" in guidance
    assert "أي مثال طلبه" in guidance
    assert "الاستشهادات الداعمة والحسابات" in guidance
    assert "المعلومات المهمة للسلامة" in guidance
