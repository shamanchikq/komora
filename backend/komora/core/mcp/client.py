"""Silpo MCP client: session wiring plus a retry policy.

Written against **mcp 2.0.0**, whose transport API differs from 1.x in ways that fail
loudly if you copy an older example:

- the function is `streamable_http_client`, not `streamablehttp_client`;
- it yields a 2-tuple `(read, write)`, not 1.x's 3-tuple;
- it has no `auth=` / `headers=` / `timeout=` parameters — auth rides on the HTTP
  client you hand it;
- that client must be **httpx2**, a different library from the `httpx` we use
  elsewhere. `OAuthClientProvider` subclasses `httpx2.Auth`.
"""

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client

from komora.core.mcp.errors import McpUnavailable, RateLimited


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    """Total tries, not extra ones."""
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.25
    """Fraction of the delay added at random, so many users do not retry in lockstep."""


DEFAULT_RETRY_POLICY = RetryPolicy()


async def with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_fraction: Callable[[], float] = random.random,
) -> T:
    """Run `operation`, retrying only failures that can plausibly resolve themselves.

    `RateLimited` and `McpUnavailable` are retried. Authentication problems are not:
    neither fixes itself, and retrying only delays telling the user to re-link.
    Anything unexpected propagates untouched rather than being retried blindly.
    """
    delay = policy.base_delay
    for attempt in range(1, policy.attempts + 1):
        try:
            return await operation()
        except (RateLimited, McpUnavailable) as exc:
            if attempt == policy.attempts:
                raise
            retry_after = getattr(exc, "retry_after", None)
            # Silpo telling us when to come back beats our own guess.
            wait = retry_after if retry_after is not None else delay
            if retry_after is None:
                wait += wait * policy.jitter * random_fraction()
                delay = min(delay * 2, policy.max_delay)
            await sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


@asynccontextmanager
async def open_session(
    server_url: str, provider: OAuthClientProvider
) -> AsyncIterator[ClientSession]:
    """Open an initialised MCP session.

    Note `auth` goes on the httpx2 client rather than the transport, and that
    `streamable_http_client` yields two streams in 2.0 where 1.x yielded three.
    """
    try:
        async with (
            httpx2.AsyncClient(auth=provider, follow_redirects=True) as http_client,
            streamable_http_client(server_url, http_client=http_client) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session
    except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.ReadTimeout) as exc:
        raise McpUnavailable(f"could not reach {server_url}: {exc}") from exc


def classify_http_error(exc: httpx2.HTTPStatusError) -> Exception:
    """Map an HTTP failure onto our own error types."""
    response = exc.response
    if response.status_code == 429:
        header = response.headers.get("retry-after")
        retry_after: float | None = None
        if header is not None:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
        return RateLimited(retry_after)
    if response.status_code >= 500:
        return McpUnavailable(f"Silpo returned {response.status_code}")
    return exc


async def call_tool(session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
    """Invoke one MCP tool, translating transport failures into our error types."""
    try:
        result = await session.call_tool(name, arguments)
    except httpx2.HTTPStatusError as exc:
        raise classify_http_error(exc) from exc
    except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.ReadTimeout) as exc:
        raise McpUnavailable(f"{name} failed: {exc}") from exc
    return result
