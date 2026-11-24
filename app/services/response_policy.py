"""Deterministic response-length guidance for chat generation."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ResponsePolicy:
    kind: str
    max_output_tokens: int
    instruction: str


_DETAIL = re.compile(r"(?:بالتفصيل|اشرح بالتفصيل|شرح شامل|detailed|in depth|comprehensive)", re.I)
_COMPARISON = re.compile(r"(?:قارن|مقارنة|الفرق بين|compare|difference between|vs\.?\b)", re.I)
_PROCEDURE = re.compile(r"(?:كيف|شلون|خطوات|طريقة|how (?:do|can|to)|steps?)", re.I)
_EDUCATIONAL = re.compile(r"(?:اشرح|فسر|وضح|حل|لماذا|explain|solve|why)\b", re.I)
_SIMPLE = re.compile(r"(?:متى|وين|منو|من هو|من هي|كم|ما هو|ما هي|when|where|who|what date|how many)", re.I)


def response_policy_for(message: str) -> ResponsePolicy:
    """Classify only the current turn so stale history cannot inflate answers."""
    text = " ".join(message.split())
    if _DETAIL.search(text):
        return ResponsePolicy("detailed", 1200, "قدّم التفاصيل المطلوبة فقط، مع تنظيم واضح ودون تكرار.")
    if _COMPARISON.search(text):
        return ResponsePolicy("comparison", 450, "أجب بمقارنة مدمجة ومنظمة، واذكر الفروق الحاسمة فقط.")
    if _PROCEDURE.search(text):
        return ResponsePolicy("procedure", 500, "أجب مباشرة في 3 إلى 5 خطوات موجزة عندما يناسب السؤال.")
    if _EDUCATIONAL.search(text):
        return ResponsePolicy("educational", 800, "اشرح بالقدر الكافي لحل السؤال، مع مثال فقط إذا كان مفيداً.")
    if _SIMPLE.search(text) or len(text) <= 60:
        return ResponsePolicy("simple", 220, "أجب في جملة أو جملتين. لا تضف مقدمة أو خاتمة أو سؤال متابعة.")
    return ResponsePolicy("standard", 600, "أجب بإيجاز ووسّع فقط بقدر ما يتطلبه السؤال الحالي.")


def response_policy_prompt(message: str) -> str:
    policy = response_policy_for(message)
    return (
        "# سياسة طول الإجابة لهذا الطلب\n"
        f"- النوع: {policy.kind}\n"
        f"- {policy.instruction}\n"
        "- ابدأ بالإجابة الفعلية، وتجنب تكرار المقدمة أو الخلاصة والمراجع غير اللازمة."
    )
