"""Narrow, deterministic answers for public KU links and audited high-risk intents."""
from dataclasses import dataclass
from fractions import Fraction
import operator
import re

STUDENT_GUIDE = "https://www.ku.edu.kw/ar/new-student-journey"
STUDENT_PORTAL = "https://portal.ku.edu.kw/sis"
STANDING_FAQ = "https://www.ku.edu.kw/sites/default/files/2025-08/FAQAr.pdf"
COURSE_RULES = "https://shi.ku.edu.kw/ar/courses-system-list"
GEMINI_WORKSHOP = "https://afaq.ku.edu.kw/ar/media-center/news/n3-61"
CIS = "https://cis.ku.edu.kw/ar/contact-us"
ARTS_ARABIC = "https://arts.ku.edu.kw/sites/default/files/2026-09/StudnetGuide25-26.pdf"
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹أإآ", "01234567890123456789ااا")
MATH = re.compile(
    r"^(?:(?:احسب|كم(?: يساوي)?|ما ناتج|calculate|what is)\s+)?"
    r"(-?[0-9]{1,9})\s*([+\-*/×÷])\s*(-?[0-9]{1,9})\s*[=؟?]?$"
)
OPS = {"+": operator.add, "-": operator.sub, "*": operator.mul, "×": operator.mul,
       "/": operator.truediv, "÷": operator.truediv}


@dataclass(frozen=True)
class VerifiedAnswer:
    text: str
    sources: tuple[tuple[str, str], ...] = ()

    def citations(self) -> list[dict]:
        return [
            {"type": "url_citation", "title": title, "url": url, "start_index": 0, "end_index": 0}
            for title, url in self.sources
        ]


def _norm(text: str) -> str:
    text = re.sub(r"[ً-ٟ]", "", text.casefold().translate(DIGITS))
    return " ".join(text.split())


def arithmetic_match(text: str):
    return MATH.fullmatch(_norm(text))


def _with_source(message: str, title: str, url: str) -> VerifiedAnswer:
    return VerifiedAnswer(f"{message} [{title}]({url})".strip(), ((title, url),))


# Split only where the second clause starts a recognizable new request.
_CONTINUATION = re.compile(
    r"(?:\s*[,،؛;]\s*|\s+)(?:(?:و|ثم\s+)(?=(?:اشرح|وضح|اعطني|عطني|علمني|كيف|شلون|متى|احسب|بين|قارن|شنو|وش|ما الفرق)\b)|"
    r"and\s+(?=(?:explain|show|how|when|calculate|give|compare)\b))",
    re.IGNORECASE,
)


def request_clauses(question: str) -> list[str]:
    return [part.strip(" ,،؛;؟?") for part in _CONTINUATION.split(_norm(question)) if part.strip(" ,،؛;؟?")]


def known_answer(question: str, prior_messages_desc: list[dict]) -> VerifiedAnswer | None:
    text = _norm(question).rstrip(" .؟?!")
    english = not re.search(r"[\u0600-\u06ff]", question)
    prior_user_turns = [_norm(str(row.get("content", ""))) for row in prior_messages_desc if row.get("isUser", True)][:4]
    previous = prior_user_turns[0] if prior_user_turns else ""

    def matches(pattern: str) -> bool:
        return re.fullmatch(pattern, text) is not None

    if match := arithmetic_match(text):
        left, op, right = match.groups()
        if op in "/÷" and int(right) == 0:
            return VerifiedAnswer("Division by zero is undefined." if english else "القسمة على صفر غير معرّفة.")
        result = OPS[op](Fraction(left), Fraction(right))
        return VerifiedAnswer(f"\x60{left} {op} {right} = {result}\x60")

    link = r"(?:الرابط|رابط|اللينك|لينك|اللنك|لنك)"
    link_request = rf"(?:(?:وين|ارسل|ابعث|اعرض|ابي|ابغى|ابغا|ممكن|عطني|اعطني|حط) (?:لي )?)?{link}"
    guide_followup = re.search(r"دليل الطالب|student guide", previous) and matches(
        rf"(?:اعرض|ارسل|حط) (?:لي )?{link}(?: في الشات)?"
    )
    if guide_followup or matches(
        rf"{link_request} دليل الطالب(?: الجديد)?(?: في الشات)?|(?:where is|send me|give me) (?:the )?(?:official )?student guide(?: link)?"
    ):
        return _with_source("", "Official new student guide" if english else "دليل الطالب الجديد على موقع جامعة الكويت", STUDENT_GUIDE)
    if matches(rf"{link_request} (?:نظام )?(?:تسجيل|تسجل) المقررات|(?:send me|give me) (?:the )?course registration link"):
        return _with_source("", "Kuwait University course registration" if english else "نظام تسجيل المقررات في جامعة الكويت", STUDENT_PORTAL)
    if matches(rf"(?:(?:السلام عليكم|سلام عليكم) )?{link_request} جامعة الكويت"):
        return _with_source("", "موقع جامعة الكويت الرسمي", "https://www.ku.edu.kw/")
    if matches(
        r"(?:(?:هل )?(?:انت(?: مساعد)?|كونيف(?: ai)?|kuniv ai|التطبيق) تابع(?: رسمي(?:ا)?)? (?:لجامعة الكويت|من جامعة الكويت|للجامعة))|"
        r"is kuniv ai (?:officially )?affiliated with kuwait university"
    ):
        return VerifiedAnswer("I’m Kuniv AI, a student assistant. I can’t verify official affiliation with Kuwait University."
                              if english else "أنا Kuniv AI، مساعد للطلبة. لا أملك ما يثبت أن التطبيق تابع رسميًا لجامعة الكويت.")

    gemini = r"(?:gemini(?: enterprise)?|جيميني(?: انتربرايز)?)"
    access_request = rf"(?:(?:شلون|كيف) (?:ادخل|اسجل)(?: على| في)?|(?:وين|كيف احصل على) رمز الدخول الى) {gemini}|how (?:do|can) i (?:sign in to|access) {gemini}"
    if matches(access_request) or (
        re.fullmatch(access_request, previous.rstrip(" .؟?!")) and matches(r"(?:وين|ما هو) (?:رمز|كود) الدخول")
    ):
        workshop_title = "KU workshop" if english else "إعلان الورشة"
        support_title = "KU IT support" if english else "مركز نظم المعلومات"
        message = (
            f"KU published a [Gemini Enterprise workshop]({GEMINI_WORKSHOP}). I couldn't verify your eligibility, sign-in link, or access-code procedure. Check with [{support_title}]({CIS})."
            if english else f"جامعة الكويت نشرت [إعلان ورشة Gemini Enterprise]({GEMINI_WORKSHOP}). ما قدرت أتحقق من أهلية حسابك أو رابط الدخول أو طريقة الحصول على الرمز. استفسر من [{support_title}]({CIS})."
        )
        return VerifiedAnswer(message, ((workshop_title, GEMINI_WORKSHOP), (support_title, CIS)))
    if matches(
        r"هل لدى جامعة الكويت ذكاء (?:اصطناعي|اصطمناعي) غيرك|"
        r"(?:في|هل يوجد) برنامج ذكاء اصطناعي لجامعة الكويت (?:شسمه|ما اسمه)|"
        r"what is kuwait university.s ai tool"
    ):
        return _with_source(
            "KU publicized a Gemini Enterprise workshop. The announcement does not establish access for every student."
            if english else "جامعة الكويت نشرت خبرًا عن ورشة **Gemini Enterprise**. الإعلان ما يثبت إتاحة المنصة لكل الطلبة.",
            "KU workshop" if english else "إعلان الورشة", GEMINI_WORKSHOP,
        )
    if matches(r"(?:عندي مشكلة في )?(?:حساب الطالب|حسابي الجامعي)(?: فيه مشكل[هة]| ما يفتح)?"):
        return VerifiedAnswer("هل المشكلة في الدخول إلى نظام تسجيل المقررات، أو في خدمة ثانية داخل حساب الطالب؟")
    if matches(r"(?:(?:اشرح لي|اشرح) )?(?:شلون|كيف) (?:احسب|يحسب) معدل التخصص|what is the major gpa formula"):
        return _with_source(
            "Major GPA is total (grade points × credits) for courses counted toward the major, divided by their total credits. Check which courses count in your degree plan."
            if english else "معدل التخصص = مجموع (نقاط كل مقرر محسوب ضمن التخصص × وحداته) ÷ مجموع وحدات هذه المقررات. راجع صحيفة تخرجك لتحديد المقررات المحتسبة.",
            "KU course regulations" if english else "لائحة نظام المقررات", COURSE_RULES,
        )
    grades = r"[a-d][+-]?(?:\s*(?:و|,|،)\s*[a-d][+-]?)*"
    grade_only = matches(
        rf"(?:هل يروح الانذار الاكاديمي لو جبت {grades}|لو جبت {grades} هل يروح الانذار الاكاديمي)|"
        r"could (?:i clear the academic warning with an? [a-d][+-]?|these grades clear academic probation)"
    )
    if grade_only and not any(re.search(r"وحد[هة]|معدل[ي ]|credits|gpa", turn) for turn in prior_user_turns):
        return _with_source(
            "Grades alone do not establish whether the warning will clear. What are your cumulative GPA, GPA credits, and credits per course? Is your warning for overall GPA or major GPA?"
            if english else "ما أقدر أحدد إذا ينرفع الإنذار من التقديرات وحدها. كم معدلك التراكمي الحالي، وكم وحدة تدخل في حسابه؟ اذكر أيضًا وحدات كل مقرر وهل الإنذار على المعدل العام أو التخصص.",
            "KU enrolled-student FAQ" if english else "الأسئلة الشائعة للطلبة المقيدين", STANDING_FAQ,
        )
    if matches(r"(?:ابي |اريد )?(?:مجرشيت|ميجر شيت|صحيف[هة] تخرج) اللغ[هة] العربي[هة] (?:بكلية )?(?:الاداب|اداب)(?: (?:للعام )?20[0-9]{2}[/\-]20[0-9]{2})?"):
        years = re.search(r"20[0-9]{2}[/\-]20[0-9]{2}", text)
        if not years or years.group().replace("-", "/") == "2025/2026":
            return _with_source("هذه صحيفة تخرج اللغة العربية بكلية الآداب **للمستجدين في 2025/2026**. إذا كانت دفعتك مختلفة، أي سنة التحقت؟", "صحيفة التخرج الرسمية", ARTS_ARABIC)
        return _with_source("ما قدرت أتحقق من صحيفة تخرج اللغة العربية بكلية الآداب للدفعة المطلوبة. تواصل مع القسم لطلب الصحيفة المطابقة.",
                            "قسم اللغة العربية وآدابها", "https://arts.ku.edu.kw/ar/departments/arabic-language-and-literature")
    if matches(r"المصطلحات والرموز الجامعي[هة]|(?:ما معنى|شنو يعني|ما هو) (?:اوفر لود|overload)"):
        return _with_source("الوحدة الدراسية تدخل في حساب العبء الدراسي. و«الأوفر لود» يعني تجاوز العبء الدراسي المعتاد، وفق الضوابط والموافقات المطبقة. إذا تقصد رمزًا في جدولك، اكتب الرمز.",
                            "لائحة نظام المقررات", COURSE_RULES)
    return None
