"""Choose an answer shape independently of the provider's safety ceiling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .verified_answers import request_clauses


@dataclass(frozen=True)
class ResponsePolicy:
    kind: str
    max_output_tokens: int
    instruction: str


_DETAIL = re.compile(r"بالتفصيل|شرح (?:شامل|مفصل)|خطوة بخطوة|detailed|in depth|in detail|comprehensive|step.by.step")
_COMPARISON = re.compile(r"قارن|مقارنة|الفرق بين|\bcompare\b|\bdifference between\b|\bvs\.?\b")
_PROCEDURE = re.compile(r"^(?:كيف|شلون|ما (?:هي )?(?:خطوات|طريقة)|how (?:do|can|to)|what are the steps)|\b(?:steps to|instructions for)\b")
_EDUCATIONAL = re.compile(
    r"(?<!\w)(?:اشرح(?:ه|ها|لي)?|فسر|وضح(?:ه|ها)?|حل|احسب|علمني|فهمني|لماذا|ليش|ب?مثال|ب?امثلة|ادرس|اذاكر|مذاكرة|"
    r"explain|clarify|solve|calculate|teach|understand|study|why|examples?|work through|walk me through)(?!\w)"
)
_FOLLOW_UP = re.compile(
    r"^(?:(?:طيب|زين|ممكن|هل ممكن|لو سمحت) )?(?:و?اشرح(?:ه|ها|لي)?|و?وضح(?:ه|ها)?|تشرح(?:ه|ها)?|توضح(?:ه|ها)?) "
    r"(?:لي )?(?:اكتر|اكثر|ابسط|بطريقة ابسط|بزيادة|زيادة|بالتفصيل|النقطة الاخيرة|الخطوة الاخيرة)\b|"
    r"^(?:(?:can|could) you )?(?:explain (?:more|further|that|it)|clarify (?:that|it)|elaborate|expand on (?:that|it)|"
    r"give (?:me )?(?:another|an) example(?=\s*[.?!]?$|\s+(?:for|of) (?:that|it)\b)|what about (?:that|the last (?:step|point)))\b|"
    r"^(?:(?:طيب|ممكن) )?(?:مثال ثاني|مثال اخر|(?:اعطني|عطني) مثال(?=\s*[.؟?]?$)|مثال على (?:هذا|كلامك))\b"
)
_FACT = re.compile(
    r"^(?:متى|وين|اين|منو|من هو|من هي|كم|هل|ما هو|ما هي|ما ابرز|شنو|وش|في منصات|ابي رابط|وين الرابط|رابط|"
    r"when|where|who|what (?:date|time|is|are)|how many|how much|does|is|can)\b"
)
_OFFICIAL = re.compile(
    r"التحويل|السحب|المقررات|التسجيل|وقف القيد|الانسحاب|الفصل الدراسي|اختبارات|موعد التقديم|القبول|"
    r"المكافاة|الانذار|سياسة|لائحة|الهوي[هة] الجامعي[هة]|جامعة الكويت|"
    r"enrollment|registration|course withdrawal|academic calendar|transfer (?:window|period)|"
    r"semester|kuwait university|admission|(?:academic|university) policy|scholarship"
)
_MULTIPLE = re.compile(
    r"(?:\s|[،,])و\s*(?:متى|وين|كم|هل|ليش|اشرح|وضح|اعطني|عطني|اديني|كيف|شلون|ما هي|شنو)\b|"
    r"\band (?:when|where|whether|explain|show|give|calculate|how|what|why)\b"
)
_LINK_ONLY = re.compile(
    r"^(?:(?:ابي|ارسل|اعطني|عطني|اريد|وين|اين|ممكن) (?:لي )?)?(?:رابط|لينك|لنك)\b|"
    r"^(?:send|give|show) me (?:the )?(?:official )?link\b"
)
_BRIEF = re.compile(r"باختصار|بجملة|جواب قصير|اختصر|\bbriefly\b|\bshort answer\b|\bin one sentence\b")


def _normalized(message: str) -> str:
    text = message.casefold().replace("ـ", "").translate(str.maketrans("أإآٱ", "اااا"))
    return " ".join(re.sub(r"[\u064b-\u065f\u0670]", "", text).split())


def has_previous_answer(history: Sequence[dict]) -> bool:
    """Expansion uses the most recent assistant answer available in the prompt."""
    return any(item.get("role") == "assistant" and str(item.get("content") or "").strip() for item in history)


def response_policy_for(message: str, *, previous_answer: bool = False) -> ResponsePolicy:
    """Use the current request; consult history only when it asks for an expansion."""
    text = _normalized(message)
    requested_detail = bool(_DETAIL.search(text))
    follow_up = bool(_FOLLOW_UP.search(text))
    mixed = len(request_clauses(message)) > 1 or bool(_MULTIPLE.search(text)) or text.count("؟") + text.count("?") > 1

    if mixed:
        emphasis = " فصّل الجزء الذي يطلب شرحاً أو مثالاً." if requested_detail or _EDUCATIONAL.search(text) else ""
        return ResponsePolicy("mixed", 2400,
            "أجب عن كل جزء بترتيب الطلب. اختصر الجزء المباشر، وأكمل أي جزء تعليمي أو تحليلي، "
            "ولا تكرر ما عرضته الخدمة ضمن المعلومة المتحققة." + emphasis)
    if follow_up and previous_answer:
        return ResponsePolicy("follow_up", 1800,
            "تابع آخر إجابة مرتبطة بالسؤال ووسّع النقطة المطلوبة بشرح إضافي أو مثال مناسب. "
            "اذكر فقط الخلفية اللازمة لفهم التوسّع، ولا تعد صياغة الإجابة كاملة.")
    if requested_detail:
        return ResponsePolicy("detailed", 2200,
            "قدّم التفاصيل المطلوبة، وبيّن الخطوات أو الحسابات وافتراضاتها. أدرج الأمثلة المطلوبة، "
            "ونظّم الشرح حسب الحاجة من دون تكرار.")
    if _COMPARISON.search(text):
        return ResponsePolicy("comparison", 1050,
            "اذكر الخلاصة أولاً ثم الفروق الحاسمة. استخدم نقاطاً قصيرة عند فائدتها، واشرح الفرق بمثال إذا طُلب.")
    # A noun like "study" in a request for a link or recommendation is
    # insufficient to turn a direct question into a lesson.
    teaching = _EDUCATIONAL.search(text)
    if follow_up or teaching and (not _FACT.search(text) or teaching.group() not in {"study", "مذاكرة", "ادرس", "اذاكر"}):
        return ResponsePolicy("educational", 1700,
            "ابدأ بالفكرة أو النتيجة. اشرح بما يكفي ليتعلم الطالب أو يعيد حل المسألة، "
            "مع الخطوات والحسابات وأي مثال طلبه. استخدم مثالاً إضافياً فقط عندما يساعد الفهم.")
    if _LINK_ONLY.search(text) and not _PROCEDURE.search(text):
        return ResponsePolicy("link", 600,
            "إذا كان المطلوب رابطاً فقط، أعطِ رابطاً قابلاً للنقر بعنوان وصفي، بلا جملة تمهيدية. "
            "أضف سطراً فقط إذا كان الرابط بديلاً أو له نطاق يهم الطالب.")
    if _PROCEDURE.search(text):
        return ResponsePolicy("procedure", 1050,
            "اذكر أقرب خطوة عملية أولاً. إذا احتاج الإجراء تسلسلاً، اذكر الخطوات اللازمة فقط بترتيبها، "
            "وغالباً تكفي 3 إلى 5 خطوات قصيرة. لا تسرد خطوات غير مؤكدة، وأبقِ شروط التنفيذ والاستثناءات.")
    if _OFFICIAL.search(text):
        return ResponsePolicy("academic", 850,
            "أعطِ الموعد أو الحكم في أول جملة، ثم نطاقه الفعلي والاستثناء المؤثر فقط. "
            "استهدف جملة أو جملتين مع رابط المصدر الرسمي. عند نقص بيانات تؤثر على الحكم، حدّد الناقص أو اذكر خطوة رسمية مفيدة؛ لا تحسب نتيجة على افتراضات غير معطاة.")
    if _FACT.search(text) or len(text.split()) <= 8:
        return ResponsePolicy("simple", 480,
            "أجب مباشرة في جملة أو جملتين عادةً. للحقيقة أو الرقم البسيط، استخدم عبارة واحدة دقيقة. "
            "مثال للصياغة: «180 دقيقة.» عند طلب قائمة عامة، اذكر مثالين أو ثلاثة من العناصر المدعومة، "
            "إلا إذا طلب الطالب قائمة كاملة. لا تعرض خلفية أو قوائم غير مطلوبة.")
    return ResponsePolicy("standard", 1150,
        "أجب عن المطلوب مباشرة دون مقدمات. أضف ما يلزم لفائدة الطالب من شرح أو حساب أو سياق.")


def response_policy_prompt(message: str, *, previous_answer: bool = False) -> str:
    policy = response_policy_for(message, previous_answer=previous_answer)
    brevity = " طلب الطالب الاختصار؛ قلّل الصياغة مع إبقاء المطلوب كاملاً." if _BRIEF.search(_normalized(message)) else ""
    return (
        "# شكل الإجابة لهذا الطلب\n"
        f"- النوع: {policy.kind}\n"
        f"- {policy.instruction}{brevity}\n"
        "- احذف المقدمة والتلخيص المعاد والخاتمة التي تعرض مزيداً من المساعدة. "
        "ابدأ بأول عبارة تقدم معلومة أو خطوة. استعمل العناوين والتعداد فقط إذا حسّنا القراءة.\n"
        "- احفظ الاستشهادات الداعمة والحسابات والأمثلة المطلوبة والمعلومات المهمة للسلامة. "
        "إذا أجبت اعتماداً على مصدر متاح، ضع رابطاً قابلاً للنقر بعد المعلومة المدعومة مرة واحدة. "
        "إذا طلب الطالب رابطاً، اجعله جوابك الأول. الاستشهاد المطلوب جزء من الجواب حتى عند الاختصار.\n"
        "- عند تعذر التحقق، اذكر السبب في جملة وأضف أقرب خطوة موثوقة إذا كانت متاحة. "
        "لا تعوّض نقص الدليل بتفاصيل تخمينية أو تفترض شاشات وإعدادات أو رموزاً محلية. "
        "إذا حددت معلومة ناقصة، لا تعِد طلبها بصياغة ثانية؛ اسأل فقط إذا كانت تغيّر الجواب."
    )
