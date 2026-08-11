"""Turning the transport's nested task groups back into an actionable error.

The MCP client runs on stacked task groups, so an unauthenticated user surfaces as an
ExceptionGroup wrapping an ExceptionGroup wrapping the real cause. Without flattening,
the bot has nothing to match on and every failure looks the same.
"""

import asyncio

from komora.core.mcp.errors import McpUnavailable, NotAuthenticated, RateLimited
from komora.core.mcp.gateway import _translated


def nested(exc: BaseException) -> BaseException:
    return ExceptionGroup("outer", [ExceptionGroup("inner", [exc])])


class TestTranslation:
    def test_a_buried_not_authenticated_is_recovered(self) -> None:
        original = NotAuthenticated("no tokens")
        assert _translated(nested(original)) is original

    def test_a_buried_rate_limit_keeps_its_type(self) -> None:
        assert isinstance(_translated(nested(RateLimited(3))), RateLimited)

    def test_a_declined_authorization_reads_as_unauthenticated(self) -> None:
        """The bridge raises PermissionError when the user taps "deny" at Silpo."""
        assert isinstance(_translated(nested(PermissionError("declined"))), NotAuthenticated)

    def test_a_timed_out_login_reads_as_unauthenticated(self) -> None:
        assert isinstance(_translated(nested(TimeoutError())), NotAuthenticated)

    def test_an_unrecognised_failure_becomes_mcp_unavailable(self) -> None:
        translated = _translated(nested(ValueError("something odd")))
        assert isinstance(translated, McpUnavailable)
        assert "something odd" in str(translated)

    def test_a_plain_exception_is_left_alone(self) -> None:
        """Nothing to flatten, so nothing to rewrite — the traceback stays honest."""
        original = ValueError("plain")
        assert _translated(original) is original


class TestCancellation:
    """A CancelledError rewritten into an McpError would break shutdown: the task would
    report a Silpo outage instead of stopping.

    `BaseExceptionGroup` — not `ExceptionGroup`, which refuses to nest a BaseException —
    is what the transport raises when a session is cancelled mid-flight, and the
    gateway's `except (Exception, BaseExceptionGroup)` does catch it.
    """

    def test_a_cancelled_session_passes_through_untouched(self) -> None:
        group = BaseExceptionGroup("transport", [asyncio.CancelledError()])
        assert _translated(group) is group

    def test_cancellation_wins_even_alongside_a_real_error(self) -> None:
        group = BaseExceptionGroup(
            "transport", [asyncio.CancelledError(), NotAuthenticated("no tokens")]
        )
        assert _translated(group) is group, "shutdown outranks reporting the cause"

    def test_a_bare_cancellation_is_not_swallowed(self) -> None:
        cancelled = asyncio.CancelledError()
        assert _translated(cancelled) is cancelled
