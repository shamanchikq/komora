"""Per-user OAuth 2.1 against Silpo's MCP server.

The SDK's `OAuthClientProvider` assumes one interactive user on a CLI. Komora serves
many Telegram users from one process, which breaks three of its assumptions:

1. `TokenStorage` conflates two lifetimes — `get/set_tokens` is per-user, but
   `get/set_client_info` is the app-wide Dynamic Client Registration. Storing the
   latter per user would register a new OAuth client with Silpo for every Telegram
   user. `DBTokenStorage` scopes them separately.
2. `_initialize()` restores tokens but not their expiry, so expired tokens look
   valid and trigger a full interactive login instead of a refresh (upstream #3250).
   `PersistentOAuthClientProvider` restores it.
3. The flow wants to open a browser and listen on localhost. We send the URL as a
   Telegram message and receive the callback on our own FastAPI endpoint, correlated
   by the `state` the SDK generates. `AuthorizationBridge` does that.

Verified against mcp 2.0.0. See docs/superpowers/specs/2026-08-10-verified-external-facts.md.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from komora.core.crypto import TokenCipher
from komora.db.repo import OAuthClientRepo, UserRepo

log = logging.getLogger(__name__)

CALLBACK_PATH = "/auth/silpo/callback"
DEFAULT_AUTH_TIMEOUT_SECONDS = 600.0

SendUrl = Callable[[int, str], Awaitable[None]]
"""Delivers the authorization URL to a user — in practice, a Telegram message."""


def is_loopback(url: str) -> bool:
    return (urlparse(url).hostname or "") in {"localhost", "127.0.0.1", "::1"}


def build_client_metadata(public_base_url: str) -> OAuthClientMetadata:
    """Client metadata for Dynamic Client Registration.

    `application_type` is chosen from the callback URL rather than hardcoded. A
    deployed Komora is a `web` client with an HTTPS callback, and leaving the SDK's
    `native` default there risks a strict authorization server rejecting the mismatch.
    But a loopback callback is `native` by definition (RFC 8252), and Silpo accepts
    one — which is why local verification needs no tunnel.
    """
    return OAuthClientMetadata(
        client_name="Komora",
        redirect_uris=[f"{public_base_url.rstrip('/')}{CALLBACK_PATH}"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
        application_type="native" if is_loopback(public_base_url) else "web",
    )


def _serves(info: OAuthClientInformationFull, redirect_uri: str) -> bool:
    """Whether a stored registration lists the callback this process would send."""
    return any(str(uri).rstrip("/") == redirect_uri.rstrip("/") for uri in info.redirect_uris or [])


class DBTokenStorage(TokenStorage):
    """Per-user tokens (encrypted) plus the one shared client registration."""

    def __init__(
        self,
        telegram_id: int,
        users: UserRepo,
        clients: OAuthClientRepo,
        cipher: TokenCipher,
        redirect_uri: str | None = None,
    ) -> None:
        self._telegram_id = telegram_id
        self._redirect_uri = redirect_uri
        """The callback this process would send. A stored registration that does not
        list it is unusable for *authorizing* — see `get_client_info`. `None` disables
        the check, which is what a session that will never authorize should pass: the
        same registration is what a token refresh signs with, and `SilpoGateway`
        enables the check only on the linking path for that reason."""
        self._users = users
        self._clients = clients
        self._cipher = cipher
        self._loaded_expiry: float | None = None

    @property
    def redirect_uri(self) -> str | None:
        """The callback a stored registration must list, or `None` when not checked."""
        return self._redirect_uri

    @property
    def loaded_expiry_timestamp(self) -> float | None:
        """Absolute expiry of the tokens last read, as a unix timestamp.

        `OAuthToken` carries only a relative `expires_in`, which is meaningless after a
        restart. `PersistentOAuthClientProvider` reads this to repair the SDK's context.
        `None` until `get_tokens()` has run, or if the token never had an expiry.
        """
        return self._loaded_expiry

    @property
    def _aad(self) -> bytes:
        """Binds a ciphertext to its owner, so a blob copied into another user's row
        fails to decrypt rather than silently granting that user's access."""
        return f"user:{self._telegram_id}".encode()

    async def get_tokens(self) -> OAuthToken | None:
        blob, expires_at = await self._users.get_token_blob(self._telegram_id)
        if blob is None:
            return None
        self._loaded_expiry = expires_at.timestamp() if expires_at else None
        return OAuthToken.model_validate_json(self._cipher.decrypt(blob, aad=self._aad))

    async def set_tokens(self, tokens: OAuthToken) -> None:
        expires_at: datetime | None = None
        if tokens.expires_in is not None:
            expiry = time.time() + int(tokens.expires_in)
            expires_at = datetime.fromtimestamp(expiry, tz=UTC)
            self._loaded_expiry = expiry
        else:
            self._loaded_expiry = None

        blob = self._cipher.encrypt(tokens.model_dump_json(), aad=self._aad)
        await self._users.set_token_blob(self._telegram_id, blob, expires_at)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """App-wide, deliberately not scoped to `self._telegram_id`.

        **Discarded when it was registered against a different callback.** The
        registration is created once and reused forever, and it carries the
        `redirect_uris` it was created with — so moving `KOMORA_PUBLIC_BASE_URL`
        (localhost to a tunnel, a tunnel to a deployment) left the stored client
        pointing at a URL this process no longer serves. Nothing detected it: the SDK
        would present a redirect_uri the authorization server never registered, and
        Silpo would refuse the flow with an OAuth error rather than anything Komora
        could explain. Returning `None` makes the SDK register afresh, which is the
        recovery `OAuthClientRepo.clear()` existed to perform by hand.

        Existing users are untouched by this only while the check stays off their
        path: a refresh signs with this very registration (`can_refresh_token` needs
        `client_info`), so the gateway asks for the check on `link()` alone and an
        ordinary `connect()` keeps whatever is stored. Once a login has re-registered,
        tokens issued to the old client stop refreshing — that is inherent to a new
        client id, and those users are asked to link again.
        """
        payload = await self._clients.get()
        if not payload:
            return None
        info = OAuthClientInformationFull.model_validate(payload)
        if self._redirect_uri is not None and not _serves(info, self._redirect_uri):
            log.info(
                "discarding the stored OAuth registration: it lists %s, this process "
                "serves %s — re-registering",
                [str(uri) for uri in info.redirect_uris or []],
                self._redirect_uri,
            )
            await self._clients.clear()
            return None
        return info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._clients.set(json.loads(client_info.model_dump_json()))


class PersistentOAuthClientProvider(OAuthClientProvider):
    """Restores the token expiry that upstream `_initialize()` drops.

    Works around mcp #3250 (open in 2.0.0). `OAuthContext.is_token_valid()` reads
    `not self.token_expiry_time`, so a `None` expiry makes an expired token look
    valid: the stale token goes out, Silpo 401s, and the SDK runs the full
    interactive flow rather than refreshing. Because Komora builds a provider per
    user per request, `_initialize()` runs constantly and silent refresh would
    essentially never happen.

    Delete this class once #3250 lands — `test_stock_provider_wrongly_reports_an_
    expired_token_as_valid` will start failing, which is the signal.
    """

    async def _initialize(self) -> None:
        await super()._initialize()
        storage = self.context.storage
        expiry = getattr(storage, "loaded_expiry_timestamp", None)
        if self.context.current_tokens is not None and expiry is not None:
            self.context.token_expiry_time = expiry


@dataclass
class _Pending:
    telegram_id: int
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: AuthorizationCodeResult | None = None
    error: str | None = None
    started: float = field(default_factory=time.monotonic)
    """When this flow was registered, for pruning the ones nobody ever collects."""


class AuthorizationBridge:
    """Routes OAuth callbacks to the coroutine waiting for them.

    `callback_handler()` takes no arguments, and the SDK generates `state` internally
    rather than letting us supply it — but it hands us the authorization URL first.
    Recovering `state` from that URL is therefore the only way to correlate an
    incoming callback with the right waiting user.

    State lives in this process. Running more than one worker requires shared storage
    (Redis pub/sub) or pinning the flow to one worker.
    """

    def __init__(self, timeout_seconds: float = DEFAULT_AUTH_TIMEOUT_SECONDS) -> None:
        self._pending: dict[str, _Pending] = {}
        self._timeout = timeout_seconds

    def pending_states(self) -> list[str]:
        return list(self._pending)

    def _prune(self) -> None:
        """Drop flows nobody can still be waiting on.

        Only `callback_handler` removes a state, in its `finally` — so a flow that dies
        between the redirect and the wait leaves its entry behind for the life of the
        process. Age is the only safe test: pruning by `telegram_id` would evict a flow
        that is still parked on its event, and the whole point of the entry is that
        somebody is waiting for it.
        """
        cutoff = time.monotonic() - self._timeout
        for state, pending in list(self._pending.items()):
            if pending.started < cutoff:
                self._pending.pop(state, None)

    def handlers(
        self, telegram_id: int, send_url: SendUrl
    ) -> tuple[Callable[[str], Awaitable[None]], Callable[[], Awaitable[AuthorizationCodeResult]]]:
        """Build the `(redirect_handler, callback_handler)` pair for one user."""
        state_holder: list[str] = []

        async def redirect_handler(authorization_url: str) -> None:
            query = parse_qs(urlparse(authorization_url).query)
            states = query.get("state") or []
            if not states:
                raise ValueError(
                    f"authorization URL carries no state parameter, so the callback "
                    f"cannot be routed back to user {telegram_id}: {authorization_url}"
                )
            state = states[0]
            # Registered only once the URL is actually on its way. `connect()` passes a
            # `send_url` that refuses outright, so an ordinary message from an unlinked
            # user reached here, filed a flow nobody would ever collect, and only then
            # raised — one leaked entry per message, for the life of the process.
            await send_url(telegram_id, authorization_url)
            state_holder.append(state)
            self._prune()
            self._pending[state] = _Pending(telegram_id=telegram_id)

        async def callback_handler() -> AuthorizationCodeResult:
            if not state_holder:
                raise RuntimeError("callback_handler ran before redirect_handler")
            state = state_holder[-1]
            pending = self._pending[state]
            try:
                await asyncio.wait_for(pending.event.wait(), timeout=self._timeout)
            finally:
                self._pending.pop(state, None)
            if pending.error is not None:
                raise PermissionError(f"Silpo declined the authorization: {pending.error}")
            # Set before the event fires, and `error` was handled above.
            assert pending.result is not None
            return pending.result

        return redirect_handler, callback_handler

    def resolve(self, state: str, code: str, iss: str | None = None) -> bool:
        """Hand a received authorization code to whoever is waiting on `state`.

        Returns False for an unknown or already-consumed state.
        """
        pending = self._pending.get(state)
        if pending is None:
            return False
        pending.result = AuthorizationCodeResult(code=code, state=state, iss=iss)
        pending.event.set()
        return True

    def fail(self, state: str, error: str) -> bool:
        """Report a declined or failed authorization.

        Without this, a user who taps "deny" leaves a coroutine parked for the full
        timeout instead of getting told immediately.
        """
        pending = self._pending.get(state)
        if pending is None:
            return False
        pending.error = error
        pending.event.set()
        return True
