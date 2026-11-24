"""
app/openai_client.py

Provides a singleton async OpenAI client.
The async client allows non-blocking API calls inside FastAPI coroutines.
"""

from functools import lru_cache
import os
import httpx

from openai import AsyncOpenAI

from .config import get_config
from .local_notification_test import enabled as local_notification_test_enabled
from .services.coordination_runtime import distributed_enabled
from .services.provider_transport import BudgetedProviderTransport


@lru_cache(maxsize=1)
def get_openai_client():
    """Return a singleton async OpenAI client."""
    if local_notification_test_enabled():
        from .local_openai_mock import client

        return client
    config = get_config()
    options = {}
    if distributed_enabled():
        size = int(os.getenv("OPENAI_CONNECTIONS_PER_WORKER", "32"))
        transport = httpx.AsyncHTTPTransport(
            limits=httpx.Limits(max_connections=size, max_keepalive_connections=size), retries=0,
        )
        options = {
            "max_retries": 0,
            "http_client": httpx.AsyncClient(transport=BudgetedProviderTransport(transport), trust_env=False),
        }
    return AsyncOpenAI(
        api_key=config.OPENAI_API_KEY,
        timeout=config.OPENAI_TIMEOUT,
        **options,
    )


async def close_openai_client() -> None:
    if get_openai_client.cache_info().currsize:
        try:
            if not local_notification_test_enabled():
                await get_openai_client().close()
        finally:
            get_openai_client.cache_clear()
