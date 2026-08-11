"""Retry/backoff policy for Silpo MCP calls.

Silpo rate-limits per user and the docs tell us to back off exponentially. The retry
core is isolated from the transport so it can be tested without a network.
"""

import pytest

from komora.core.mcp.client import RetryPolicy, with_retry
from komora.core.mcp.errors import (
    McpUnavailable,
    NotAuthenticated,
    OAuthRecoveryNeeded,
    RateLimited,
)


class Recorder:
    """Stands in for asyncio.sleep so backoff is asserted, not waited for."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def failing(times: int, error: Exception, result: str = "ok"):
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        if calls["n"] <= times:
            raise error
        return result

    operation.calls = calls  # type: ignore[attr-defined]
    return operation


class TestWithRetry:
    async def test_returns_immediately_on_success(self) -> None:
        sleep = Recorder()
        operation = failing(0, RateLimited())
        assert await with_retry(operation, sleep=sleep) == "ok"
        assert sleep.delays == [], "no backoff when nothing failed"

    async def test_retries_rate_limiting_then_succeeds(self) -> None:
        sleep = Recorder()
        operation = failing(2, RateLimited())
        assert await with_retry(operation, sleep=sleep) == "ok"
        assert operation.calls["n"] == 3
        assert len(sleep.delays) == 2

    async def test_backoff_grows(self) -> None:
        sleep = Recorder()
        policy = RetryPolicy(attempts=4, base_delay=1.0, jitter=0.0)
        with pytest.raises(RateLimited):
            await with_retry(failing(99, RateLimited()), policy=policy, sleep=sleep)
        assert sleep.delays == [1.0, 2.0, 4.0], "exponential, one gap short of the attempts"

    async def test_retry_after_overrides_backoff(self) -> None:
        """Silpo telling us when to come back beats our own guess."""
        sleep = Recorder()
        policy = RetryPolicy(attempts=2, base_delay=1.0, jitter=0.0)
        with pytest.raises(RateLimited):
            await with_retry(failing(99, RateLimited(retry_after=42.0)), policy=policy, sleep=sleep)
        assert sleep.delays == [42.0]

    async def test_backoff_is_capped(self) -> None:
        sleep = Recorder()
        policy = RetryPolicy(attempts=6, base_delay=10.0, max_delay=15.0, jitter=0.0)
        with pytest.raises(RateLimited):
            await with_retry(failing(99, RateLimited()), policy=policy, sleep=sleep)
        assert max(sleep.delays) == 15.0

    async def test_jitter_stays_within_bounds(self) -> None:
        """Jitter spreads retries out so many users do not stampede together."""
        sleep = Recorder()
        policy = RetryPolicy(attempts=4, base_delay=1.0, jitter=0.5)
        with pytest.raises(RateLimited):
            await with_retry(failing(99, RateLimited()), policy=policy, sleep=sleep)
        for delay, plain in zip(sleep.delays, [1.0, 2.0, 4.0], strict=True):
            assert plain <= delay <= plain * 1.5

    async def test_unavailability_is_retried(self) -> None:
        sleep = Recorder()
        assert await with_retry(failing(1, McpUnavailable("boom")), sleep=sleep) == "ok"

    async def test_gives_up_after_the_configured_attempts(self) -> None:
        sleep = Recorder()
        operation = failing(99, RateLimited())
        with pytest.raises(RateLimited):
            await with_retry(operation, policy=RetryPolicy(attempts=3), sleep=sleep)
        assert operation.calls["n"] == 3, "attempts counts total tries, not extra ones"

    @pytest.mark.parametrize(
        "error", [NotAuthenticated("link first"), OAuthRecoveryNeeded("re-register")]
    )
    async def test_auth_failures_are_not_retried(self, error: Exception) -> None:
        """Retrying these wastes the user's time — neither fixes itself."""
        sleep = Recorder()
        operation = failing(99, error)
        with pytest.raises(type(error)):
            await with_retry(operation, sleep=sleep)
        assert operation.calls["n"] == 1
        assert sleep.delays == []

    async def test_unexpected_errors_propagate_untouched(self) -> None:
        sleep = Recorder()
        operation = failing(99, ValueError("programmer error"))
        with pytest.raises(ValueError, match="programmer error"):
            await with_retry(operation, sleep=sleep)
        assert operation.calls["n"] == 1
