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
    return " ".join(text.casefold().translate(DIGITS).split())


def arithmetic_match(text: str):
    return MATH.fullmatch(_norm(text))


def _with_source(message: str, title: str, url: str) -> VerifiedAnswer:
    return VerifiedAnswer(f"{message} [{title}]({url})".strip(), ((title, url),))


def known_answer(question: str, prior_messages_desc: list[dict]) -> VerifiedAnswer | None:
    text = _norm(question)
    english = not re.search(r"[\u0600-\u06ff]", question)
    previous = next((_norm(str(row.get("content", ""))) for row in prior_messages_desc if row.get("isUser", True)), "")
    asks_link = bool(re.search(r"رابط|لينك|لنك|\blink\b|\burl\b", text))
    prior_guide = bool(re.search(r"دليل الطالب|student guide", previous))
    if match := arithmetic_match(text):
        left, op, right = match.groups()
        if op in "/÷" and int(right) == 0:
            return VerifiedAnswer("Division by zero is undefined." if english else "القسمة على صفر غير معرّفة.")
        return VerifiedAnswer(f"\x60{left} {op} {right} = {OPS[op](Fraction(left), Fraction(right))}\x60")
    if asks_link and (
        re.search(r"دليل الطالب|student guide", text) or
        (prior_guide and re.fullmatch(r"(?:اعرض|ارسل|حط)\s+(?:لي\s+)?(?:الرابط|اللنك|اللينك)(?:\s+في الشات)?[؟?]?", text))
    ):
        return _with_source("", "Official new student guide" if english else "دليل الطالب الجديد على موقع جامعة الكويت", STUDENT_GUIDE)
    if asks_link and re.search(r"(?:تسج[ي]?ل|تسجل) المقررات|course registration|student information system", text):
        return _with_source("", "Kuwait University course registration" if english else "نظام تسجيل المقررات في جامعة الكويت", STUDENT_PORTAL)
    if asks_link and re.search(r"جامعة الكويت|kuwait university", text) and not re.search(r"كنيف|kuniv|التقديم|القبول|المكتبة|الحساب|admission|library|account", text):
        if re.fullmatch(r"(?:(?:السلام عليكم|سلام عليكم) )?(?:(?:ابي|اعطني|ارسل|ممكن|عطني) )?(?:الرابط|رابط|لينك|لنك) جامعة الكويت[؟?]?", text):
            return _with_source("", "موقع جامعة الكويت الرسمي", "https://www.ku.edu.kw/")
    if re.search(r"تابع لجامعة الكويت|تابع للجامعة|رسمي من جامعة الكويت|affiliated with kuwait university", text):
        return VerifiedAnswer("I’m Kuniv AI, a student assistant. I can’t verify official affiliation with Kuwait University."
                              if english else "أنا Kuniv AI، مساعد للطلبة. لا أملك ما يثبت أن التطبيق تابع رسميًا لجامعة الكويت.")
    gemini = bool(re.search(r"gemini|جيميني", text))
    access = bool(re.search(r"ادخل|الدخول|رمز|كود|تسجيل|access|sign.?in|code", text))
    ku_ai_tool = bool(re.search(
        r"ذكاء (?:اصطناعي|اصطمناعي).{0,25}(?:غيرك|لجامعة الكويت)|"
        r"(?:جامعة الكويت|الجامعه).{0,30}ذكاء (?:اصطناعي|اصطمناعي)|kuwait university.{0,20}ai tool",
        text,
    ))
    if ku_ai_tool or (gemini and (access or re.search(r"جامعة الكويت|kuwait university|شنو|شسمه", text))) or (
        re.search(r"gemini|جيميني", previous) and access and len(text.split()) < 8
    ):
        if access:
            workshop_title = "KU workshop" if english else "إعلان الورشة"
            support_title = "KU IT support" if english else "مركز نظم المعلومات"
            message = (
                f"KU published a [Gemini Enterprise workshop]({GEMINI_WORKSHOP}). I couldn't verify your eligibility, sign-in link, or access-code procedure. Check with [{support_title}]({CIS})."
                if english else f"جامعة الكويت نشرت [إعلان ورشة Gemini Enterprise]({GEMINI_WORKSHOP}). ما قدرت أتحقق من أهلية حسابك أو رابط الدخول أو طريقة الحصول على الرمز. استفسر من [{support_title}]({CIS})."
            )
            return VerifiedAnswer(message, ((workshop_title, GEMINI_WORKSHOP), (support_title, CIS)))
        return _with_source(
            "KU publicized a Gemini Enterprise workshop. The announcement does not establish access for every student."
            if english else "جامعة الكويت نشرت خبرًا عن ورشة **Gemini Enterprise**. الإعلان ما يثبت إتاحة المنصة لكل الطلبة.",
            "KU workshop" if english else "إعلان الورشة", GEMINI_WORKSHOP,
        )
    if re.fullmatch(r"(?:عندي مشكلة في )?(?:حساب الطالب|حسابي الجامعي)(?: فيه مشكل[هة]| ما يفتح)?[.؟?]?", text):
        return VerifiedAnswer("هل المشكلة في الدخول إلى نظام تسجيل المقررات، أو في خدمة ثانية داخل حساب الطالب؟")
    if re.search(r"(?:شلون|كيف).{0,20}(?:احسب|حساب).{0,10}معدل التخصص", text):
        return _with_source("معدل التخصص = مجموع (نقاط كل مقرر محسوب ضمن التخصص × وحداته) ÷ مجموع وحدات هذه المقررات. راجع صحيفة تخرجك لتحديد المقررات المحتسبة.",
                            "لائحة نظام المقررات", COURSE_RULES)
    if re.search(r"يروح|يزول|اشيل|ازيل|رفع|حصلت|جبت|درجات|clear|remove|lift", text) and re.search(r"انذار|probation|academic warning", text):
        if english:
            return _with_source("Grades alone do not establish whether the warning will clear. What are your cumulative GPA, GPA credits, and credits per course? Is your warning for overall GPA or major GPA?", "KU enrolled-student FAQ", STANDING_FAQ)
        return _with_source("ما أقدر أحدد إذا ينرفع الإنذار من التقديرات وحدها. كم معدلك التراكمي الحالي، وكم وحدة تدخل في حسابه؟ اذكر أيضًا وحدات كل مقرر وهل الإنذار على المعدل العام أو التخصص.",
                            "الأسئلة الشائعة للطلبة المقيدين", STANDING_FAQ)
    if re.search(r"مجرشيت|ميجر شيت|صحيف[هة] تخرج|major sheet", text) and re.search(r"اللغ[هة] العربي[هة]", text) and re.search(r"الاداب|اداب", text):
        if not re.search(r"20(?:2[0-46-9]|3[0-9])", text.replace("2025/2026", "")):
            return _with_source("هذه صحيفة تخرج اللغة العربية بكلية الآداب **للمستجدين في 2025/2026**. إذا كانت دفعتك مختلفة، أي سنة التحقت؟", "صحيفة التخرج الرسمية", ARTS_ARABIC)
        return VerifiedAnswer("ما قدرت أتحقق من صحيفة تخرج اللغة العربية بكلية الآداب لسنة التحاقك. أي دفعة تقصد؟")
    if re.fullmatch(r"المصطلحات والرموز الجامعي[هة][؟?]?", text) or re.fullmatch(r"(?:ما معنى|شنو يعني|ما هو) (?:اوفر لود|overload)[؟?]?", text):
        return _with_source("الوحدة الدراسية تدخل في حساب العبء الدراسي. و«الأوفر لود» يعني تجاوز العبء الدراسي المعتاد، وفق الضوابط والموافقات المطبقة. إذا تقصد رمزًا في جدولك، اكتب الرمز.",
                            "لائحة نظام المقررات", COURSE_RULES)
    return None
