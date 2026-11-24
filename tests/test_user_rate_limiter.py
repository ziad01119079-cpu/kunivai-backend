import asyncio
import pytest

from app.services.user_rate_limiter import UserRateLimiter, UserRateLimitExceeded


def test_limits_each_authenticated_user_independently():
    limiter = UserRateLimiter(requests=2, window_seconds=60)

    async def run():
        await limiter.check("user-a", now=0)
        await limiter.check("user-a", now=1)
        await limiter.check("user-b", now=1)
        with pytest.raises(UserRateLimitExceeded):
            await limiter.check("user-a", now=2)

    asyncio.run(run())


def test_window_expiry_restores_capacity():
    limiter = UserRateLimiter(requests=1, window_seconds=10)

    async def run():
        await limiter.check("user", now=0)
        with pytest.raises(UserRateLimitExceeded):
            await limiter.check("user", now=9)
        await limiter.check("user", now=10)

    asyncio.run(run())
