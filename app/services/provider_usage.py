"""Content-free telemetry for provider usage observed by the application."""
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import re
from uuid import uuid4

from ..history_builder import _estimate_tokens

logger = logging.getLogger(__name__)
# Standard USD per million text tokens. Unknown models and missing usage stay unknown.
PRICE_VERSION = "2026-11-25-standard"
RATES = {"gpt-4o": (2.50, 1.25, 10.00), "gpt-4o-mini": (0.15, 0.075, 0.60)}
SNAPSHOTS = {
    "gpt-4o-2024-08-06": "gpt-4o",
    "gpt-4o-2024-11-20": "gpt-4o",
    "gpt-4o-mini-2024-07-18": "gpt-4o-mini",
}


def log_usage(event: str, fields: dict) -> None:
    logger.info(event, extra={"cost_telemetry": True, "operational_metadata": fields})


def count(value):
    return value if type(value) is int and value >= 0 else None


def token_fields(usage, *, responses: bool) -> dict:
    prompt_field, output_field = ("input_tokens", "output_tokens") if responses else ("prompt_tokens", "completion_tokens")
    details = getattr(usage, prompt_field + "_details", None)
    return {
        "input_tokens": count(getattr(usage, prompt_field, None)),
        "output_tokens": count(getattr(usage, output_field, None)),
        "cached_tokens": count(getattr(details, "cached_tokens", None)),
    }


def estimate_cost(model: str | None, tokens: dict, file_search: int | None, web_search: int | None,
                  *, preview: bool = False) -> float | None:
    rate = RATES.get(SNAPSHOTS.get(model, model))
    incoming, outgoing, cached = (tokens[k] for k in ("input_tokens", "output_tokens", "cached_tokens"))
    if rate is None or incoming is None or outgoing is None or file_search is None or web_search is None:
        return None
    cached = min(cached or 0, incoming)
    text = ((incoming - cached) * rate[0] + cached * rate[1] + outgoing * rate[2]) / 1_000_000
    return round(text + file_search * 0.0025 + web_search * (0.025 if preview else 0.01), 9)


def input_breakdown(messages: list[dict], prompt: str, model: str) -> dict:
    """Count client-side text, independently per component, without retaining it."""
    def tokens(value: str) -> int:
        return _estimate_tokens(value, model)
    schedule = re.search(r"^<student_schedule_data>\n(.*?)\n</student_schedule_data>$", prompt, re.M | re.S)
    context = re.search(r"^<user_provided_context>\n(.*?)\n</user_provided_context>$", prompt, re.M | re.S)
    core = prompt
    if schedule:
        core = core.replace(schedule.group(0), "", 1)
    if context:
        core = core.replace(context.group(0), "", 1)
    values = [str(message.get("content", "")) for message in messages]
    try:
        return {
            "system_text_tokens_est": tokens(core),
            "schedule_text_tokens_est": tokens(schedule.group(1)) if schedule else 0,
            "client_context_text_tokens_est": tokens(context.group(1)) if context else 0,
            "history_text_tokens_est": sum(tokens(value) for value in values[:-1]),
            "current_question_text_tokens_est": tokens(values[-1]) if values else 0,
        }
    except (OSError, ValueError):
        return {}


@dataclass
class UsageTrace:
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    route: str = "unknown"
    fallback_reason: str | None = None
    attempts: list[dict] = field(default_factory=list)

    def summary(self, phase: str, **timing) -> None:
        estimates = [attempt["estimated_cost_usd"] for attempt in self.attempts]
        known_tokens = {key: 0 for key in ("input_tokens", "output_tokens", "cached_tokens")}
        for attempt in self.attempts:
            for key in known_tokens:
                known_tokens[key] += attempt[key] or 0
        log_usage("provider_request_usage", {
            "trace_id": self.trace_id, "phase": phase, "route": self.route,
            "attempts": len(estimates), "unpriced_attempts": estimates.count(None),
            "known_tokens": known_tokens,
            "attempts_with_cached_token_usage": sum(a["cached_tokens"] is not None for a in self.attempts),
            "file_search_calls_observed": sum(a["file_search_calls_observed"] for a in self.attempts),
            "web_search_calls_observed": sum(a["web_search_calls_observed"] for a in self.attempts),
            "estimated_cost_known_attempts_usd": round(sum(e for e in estimates if e is not None), 9),
            "estimated_cost_usd": None if None in estimates else round(sum(estimates), 9),
            "price_version": PRICE_VERSION, **timing,
        })


current_usage: ContextVar[UsageTrace | None] = ContextVar("current_usage", default=None)


@dataclass
class ProviderAttempt:
    requested_model: str
    operation: str
    components: dict = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    retry_reason: str | None = None
    response: object | None = None
    outcome: str = "interrupted"
    terminal_response: bool = False
    _calls: dict[str, set[str]] = field(default_factory=lambda: {"file_search_call": set(), "web_search_call": set()})

    def observe_tool(self, item) -> None:
        kind = getattr(item, "type", None)
        status = getattr(item, "status", None)
        call_id = getattr(item, "id", None)
        if kind in self._calls and status in {"completed", "failed", "incomplete"} and isinstance(call_id, str):
            self._calls[kind].add(call_id)

    def finish(self) -> None:
        trace = current_usage.get()
        raw_model = getattr(self.response, "model", None)
        actual_model = raw_model if isinstance(raw_model, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", raw_model) else None
        usage = token_fields(getattr(self.response, "usage", None), responses=self.operation == "responses")
        file_texts = []
        file_results_seen = False
        for item in getattr(self.response, "output", None) or []:
            self.observe_tool(item)
            if getattr(item, "type", None) == "file_search_call" and isinstance(getattr(item, "results", None), list):
                file_results_seen = True
                file_texts.extend(result.text for result in item.results if isinstance(getattr(result, "text", None), str))
        file_result_tokens = None
        if file_results_seen:
            try:
                file_result_tokens = sum(_estimate_tokens(text, self.requested_model) for text in file_texts)
            except (OSError, ValueError):
                pass
        file_calls, web_calls = (len(self._calls[k]) for k in self._calls)
        terminal_outputs = self.terminal_response and isinstance(getattr(self.response, "output", None), list)
        tool_coverage = not self.tools or terminal_outputs
        file_fee = file_calls if tool_coverage else None
        web_fee = web_calls if tool_coverage else None
        # Usage residual includes hosted content, hidden tool instructions,
        # message framing and tokenizer effects. Do not label it a provider split.
        residual = usage["input_tokens"] - sum(self.components.values()) if (
            self.components and usage["input_tokens"] is not None
        ) else None
        metadata = {
            "trace_id": trace.trace_id if trace else uuid4().hex,
            "attempt": len(trace.attempts) + 1 if trace else 1,
            "operation": self.operation, "requested_model": self.requested_model,
            "actual_model": actual_model, "outcome": self.outcome,
            "fallback_reason": trace.fallback_reason if trace and self.operation == "responses" else None,
            "retry_reason": self.retry_reason, **usage, **self.components,
            "tools_offered": list(self.tools), "file_search_calls_observed": file_calls,
            "web_search_calls_observed": web_calls, "tool_coverage": "not_offered" if not self.tools else "terminal_output" if tool_coverage else "partial",
            "file_search_returned_passage_tokens_est": file_result_tokens,
            "file_search_returned_passages": len(file_texts) if file_results_seen else None,
            "provider_input_residual_tokens_est": residual,
            "cached_token_pricing_basis": "assume_uncached" if usage["cached_tokens"] is None else "reported",
            "price_version": PRICE_VERSION,
            "estimated_cost_usd": estimate_cost(actual_model, usage, file_fee, web_fee, preview="web_search_preview" in self.tools),
        }
        if trace:
            trace.attempts.append(metadata)
        log_usage("provider_attempt_usage", metadata)
