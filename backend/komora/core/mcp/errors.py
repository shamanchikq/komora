"""Errors raised by the Silpo MCP client."""


class McpError(Exception):
    """Base for everything this package raises."""


class NotAuthenticated(McpError):
    """This user has no usable Silpo tokens and must link their account.

    Raised instead of starting an interactive login, because the SDK runs the whole
    flow while holding `context.lock` inside the HTTP request lifecycle — a tool call
    that triggered re-auth would block a coroutine for the user's entire login time.
    Account linking is its own explicit flow.
    """


class RateLimited(McpError):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(f"Silpo rate limited us (retry after {retry_after}s)")
        self.retry_after = retry_after


class McpUnavailable(McpError):
    """Silpo's MCP server could not be reached, or kept failing after retries."""


class OAuthRecoveryNeeded(McpError):
    """Our Dynamic Client Registration is no longer accepted.

    The shared registration row must be wiped so the next attempt re-registers;
    without that the client can never recover from an expired DCR secret (mcp #3256).
    """
