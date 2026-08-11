"""Opening a Silpo session for a given Telegram user.

Everything per-user about OAuth converges here: which storage a provider gets, what
happens when a user has no tokens, and how the transport's nested task groups are
turned back into an error the bot can act on.

**A tool call never starts a login.** The SDK would happily run the whole interactive
flow inside the request, holding `context.lock` for as long as the user takes to sign
in. Instead `connect()` raises `NotAuthenticated` and account linking is its own
explicit, backgrounded flow.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from komora.core.crypto import TokenCipher
from komora.core.mcp.auth import (
    AuthorizationBridge,
    DBTokenStorage,
    PersistentOAuthClientProvider,
    SendUrl,
    build_client_metadata,
)
from komora.core.mcp.client import open_session
from komora.core.mcp.errors import McpError, McpUnavailable, NotAuthenticated
from komora.core.mcp.payload import root_causes
from komora.core.mcp.silpo import SilpoSession
from komora.db.repo import OAuthClientRepo, UserRepo


class SilpoGateway:
    def __init__(
        self,
        *,
        server_url: str,
        public_base_url: str,
        users: UserRepo,
        clients: OAuthClientRepo,
        cipher: TokenCipher,
        bridge: AuthorizationBridge,
    ) -> None:
        self._server_url = server_url
        self._metadata = build_client_metadata(public_base_url)
        self._users = users
        self._clients = clients
        self._cipher = cipher
        self._bridge = bridge

    def _provider(self, telegram_id: int, send_url: SendUrl) -> PersistentOAuthClientProvider:
        """A fresh provider per session.

        Caching one per user would save two discovery requests per message, but it also
        caches the tokens it loaded — so a user who has just linked their account would
        keep hitting the state from before. Correctness first; this is the obvious
        optimisation if latency ever matters.
        """
        redirect_handler, callback_handler = self._bridge.handlers(telegram_id, send_url)
        return PersistentOAuthClientProvider(
            server_url=self._server_url,
            client_metadata=self._metadata,
            storage=DBTokenStorage(
                telegram_id=telegram_id,
                users=self._users,
                clients=self._clients,
                cipher=self._cipher,
            ),
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )

    @asynccontextmanager
    async def connect(self, telegram_id: int) -> AsyncIterator[SilpoSession]:
        """A session for ordinary work. Raises `NotAuthenticated` if unlinked."""

        async def refuse(_: int, __: str) -> None:
            raise NotAuthenticated(f"user {telegram_id} has no usable Silpo tokens")

        async with self._session(telegram_id, refuse) as session:
            yield session

    async def link(self, telegram_id: int, send_url: SendUrl) -> None:
        """Run account linking to completion.

        Blocks for as long as the user takes to sign in — up to the bridge's timeout —
        so callers run it as a background task. The `list_tools` call at the end is
        what proves the tokens actually work before we tell the user they are linked.
        """
        async with self._session(telegram_id, send_url) as session:
            await session.list_tools()

    async def is_linked(self, telegram_id: int) -> bool:
        blob, _ = await self._users.get_token_blob(telegram_id)
        return blob is not None

    @asynccontextmanager
    async def _session(self, telegram_id: int, send_url: SendUrl) -> AsyncIterator[SilpoSession]:
        provider = self._provider(telegram_id, send_url)
        try:
            async with open_session(self._server_url, provider) as raw:
                yield SilpoSession(raw)
        except (Exception, BaseExceptionGroup) as exc:
            translated = _translated(exc)
            if translated is exc:
                raise
            raise translated from exc


def _translated(exc: BaseException) -> BaseException:
    """Recover the real cause from the transport's nested task groups.

    An unauthenticated user otherwise surfaces as an `ExceptionGroup` wrapping an
    `ExceptionGroup` wrapping the actual error, and the bot has nothing to match on.
    """
    causes = root_causes(exc)

    # Cancellation and other BaseExceptions must not be rewritten into an McpError:
    # swallowing a CancelledError breaks shutdown.
    if any(not isinstance(cause, Exception) for cause in causes):
        return exc

    for cause in causes:
        if isinstance(cause, McpError):
            return cause
    for cause in causes:
        if isinstance(cause, PermissionError):
            # The bridge raises this when the user declines at Silpo's consent screen.
            return NotAuthenticated(str(cause))
        if isinstance(cause, asyncio.TimeoutError | TimeoutError):
            return NotAuthenticated("authorization timed out")

    if len(causes) == 1 and not isinstance(exc, BaseExceptionGroup):
        return exc
    return McpUnavailable("; ".join(f"{type(c).__name__}: {c}" for c in causes)[:300])
