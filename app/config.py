"""
app/config.py

Reads and validates all required environment variables at startup.
Set these on Railway via the Railway dashboard → Variables tab.
"""
import os
from functools import lru_cache
from dotenv import load_dotenv

from .local_notification_test import enabled, validate_environment

# A local test process must never load the repository's normal .env file.
if not enabled():
    load_dotenv()
class Config:
    # ── OpenAI ────────────────────────────────────────────────────────────
    OPENAI_API_KEY: str
    OPENAI_VECTOR_STORE_ID: str
    # gpt-4o | gpt-4o-mini | etc.  Override via env var to change model.
    OPENAI_MODEL: str
    # Vision-capable model used to extract student schedule PDFs.
    OPENAI_SCHEDULE_MODEL: str

    # ── Firebase ──────────────────────────────────────────────────────────
    # Paste the entire service-account JSON as a single-line string.
    # Railway → Variables → FIREBASE_SERVICE_ACCOUNT_JSON = '{"type":"service_account",...}'
    FIREBASE_SERVICE_ACCOUNT_JSON: str | None

    # ── Chat history ──────────────────────────────────────────────────────
    # Rough token budget for conversation history injected into each request.
    HISTORY_TOKEN_BUDGET: int

    def __init__(self) -> None:
        if enabled():
            validate_environment()
            self.OPENAI_API_KEY = "local-mock-no-key"
            self.OPENAI_VECTOR_STORE_ID = "vs-local-mock"
        else:
            self.OPENAI_API_KEY = _require("OPENAI_API_KEY")
            self.OPENAI_VECTOR_STORE_ID = _require("OPENAI_VECTOR_STORE_ID")
        self.OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.OPENAI_ROUTE_MODELS_ENABLED = os.getenv(
            "OPENAI_ROUTE_MODELS_ENABLED", "false"
        ).strip().lower() in {"true", "1", "yes"}
        self.OPENAI_MODEL_DEFAULT = os.getenv("OPENAI_MODEL_DEFAULT", "").strip()
        self.OPENAI_MODEL_RAG = os.getenv("OPENAI_MODEL_RAG", "").strip()
        self.OPENAI_MODEL_RAG_FALLBACK = os.getenv("OPENAI_MODEL_RAG_FALLBACK", "").strip()
        self.OPENAI_MODEL_DETAILED = os.getenv("OPENAI_MODEL_DETAILED", "").strip()
        self.OPENAI_REASONING_EFFORT_DEFAULT = os.getenv("OPENAI_REASONING_EFFORT_DEFAULT", "none").strip()
        self.OPENAI_REASONING_EFFORT_DETAILED = os.getenv("OPENAI_REASONING_EFFORT_DETAILED", "low").strip()
        if self.OPENAI_ROUTE_MODELS_ENABLED and {
            self.OPENAI_REASONING_EFFORT_DEFAULT, self.OPENAI_REASONING_EFFORT_DETAILED
        } - {"none", "low"}:
            raise ValueError("Chat reasoning effort must be none or low")
        self.OPENAI_SCHEDULE_MODEL = os.getenv(
            "OPENAI_SCHEDULE_MODEL", self.OPENAI_MODEL
        )
        self.FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv(
            "FIREBASE_SERVICE_ACCOUNT_JSON"
        )
        self.HISTORY_TOKEN_BUDGET = int(
            os.getenv("HISTORY_TOKEN_BUDGET", "3000")
        )
        self.OPENAI_TIMEOUT = float(
            os.getenv("OPENAI_TIMEOUT", "60.0")
        )
        # ── Community Knowledge (disabled by default, fail-closed) ────────
        self.COMMUNITY_INGESTION_ENABLED = os.getenv(
            "COMMUNITY_INGESTION_ENABLED", "false"
        ).strip().lower() in {"true", "1", "yes"}
        self.COMMUNITY_RAG_ENABLED = os.getenv(
            "COMMUNITY_RAG_ENABLED", "false"
        ).strip().lower() in {"true", "1", "yes"}
        self.COMMUNITY_FAIL_CLOSED = os.getenv(
            "COMMUNITY_FAIL_CLOSED", "true"
        ).strip().lower() not in {"false", "0", "no"}
        raw_min_conf = float(os.getenv("COMMUNITY_SOURCE_MIN_CONFIDENCE", "0.0"))
        self.COMMUNITY_SOURCE_MIN_CONFIDENCE = max(0.0, min(1.0, raw_min_conf))


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            "Add it in Railway → Variables."
        )
    return value


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Return a singleton Config instance (validated once on first call)."""
    return Config()
