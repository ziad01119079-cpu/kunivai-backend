"""Opt-in model routing for student chat; other provider calls keep their model."""

from .response_policy import ResponsePolicy


_REASONING_MODELS = {"gpt-6-luna", "gpt-5.4-mini", "gpt-5.4-mini-2026-03-17"}
_SUPPORTED_EFFORTS = {"none", "low"}


def reasoning_for(model: str, effort: str | None) -> str | None:
    if model not in _REASONING_MODELS:
        return None
    if effort not in _SUPPORTED_EFFORTS and effort is not None:
        raise ValueError("Chat reasoning effort must be none or low")
    return effort


def model_options(config, policy: ResponsePolicy, *, rag: bool, fallback: bool = False) -> dict:
    if not getattr(config, "OPENAI_ROUTE_MODELS_ENABLED", False):
        return {}
    default = config.OPENAI_MODEL_DEFAULT or config.OPENAI_MODEL
    detailed = policy.kind in {"detailed", "educational", "follow_up"} or policy.teaching
    if rag:
        model = ((config.OPENAI_MODEL_RAG_FALLBACK if fallback else None)
                 or config.OPENAI_MODEL_RAG or default)
    elif detailed:
        model = config.OPENAI_MODEL_DETAILED or default
    else:
        model = default
    effort = config.OPENAI_REASONING_EFFORT_DETAILED if detailed else config.OPENAI_REASONING_EFFORT_DEFAULT
    return {"model": model, "reasoning_effort": reasoning_for(model, effort)}


def effective_max_output_tokens(policy: ResponsePolicy, *, hosted_tools: bool, effort: str | None) -> int:
    """Keep the visible answer policy separate from provider execution headroom."""
    ceiling = max(policy.max_output_tokens, 850) + 180 if hosted_tools else policy.max_output_tokens
    return ceiling + (600 if effort == "low" else 0)
