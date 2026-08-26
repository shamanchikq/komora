"""Per-user OAuth against Silpo's MCP server.

The SDK's OAuthClientProvider is built for a single interactive CLI user. Komora
serves many Telegram users from one process, which breaks three of its assumptions —
each has a test here.
"""

import asyncio
import base64
import os
import time

import pytest
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy.ext.asyncio import async_sessionmaker

from komora.core.crypto import TokenCipher
from komora.core.mcp.auth import (
    AuthorizationBridge,
    DBTokenStorage,
    PersistentOAuthClientProvider,
    build_client_metadata,
)
from komora.core.mcp.errors import NotAuthenticated
from komora.db.repo import OAuthClientRepo, UserRepo

KEY = base64.urlsafe_b64encode(os.urandom(32)).decode()
USER, OTHER_USER = 4242, 9999
SERVER = "https://mcp.silpo.ua/mcp"


def storage_for(
    sessions: async_sessionmaker,
    telegram_id: int = USER,
    redirect_uri: str | None = None,
) -> DBTokenStorage:
    return DBTokenStorage(
        telegram_id=telegram_id,
        users=UserRepo(sessions),
        clients=OAuthClientRepo(sessions),
        cipher=TokenCipher(KEY),
        redirect_uri=redirect_uri,
    )


def a_token(**kw: object) -> OAuthToken:
    return OAuthToken.model_validate(
        {"access_token": "at", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rt"}
        | kw
    )


class TestDBTokenStorage:
    async def test_no_tokens_before_linking(self, sessions: async_sessionmaker) -> None:
        assert await storage_for(sessions).get_tokens() is None

    async def test_token_roundtrip(self, sessions: async_sessionmaker) -> None:
        storage = storage_for(sessions)
        await storage.set_tokens(a_token())
        loaded = await storage.get_tokens()
        assert loaded is not None
        assert loaded.access_token == "at"
        assert loaded.refresh_token == "rt"

    async def test_tokens_are_encrypted_at_rest(self, sessions: async_sessionmaker) -> None:
        await storage_for(sessions).set_tokens(a_token())
        blob, _ = await UserRepo(sessions).get_token_blob(USER)
        assert blob is not None
        assert b"at" not in blob and b"rt" not in blob

    async def test_tokens_are_bound_to_their_owner(self, sessions: async_sessionmaker) -> None:
        """A blob copied into another user's row must not decrypt into their session."""
        await storage_for(sessions, USER).set_tokens(a_token())
        blob, expires = await UserRepo(sessions).get_token_blob(USER)
        assert blob is not None
        await UserRepo(sessions).set_token_blob(OTHER_USER, blob, expires)

        with pytest.raises(Exception):  # noqa: B017
            await storage_for(sessions, OTHER_USER).get_tokens()

    async def test_absolute_expiry_is_persisted_for_reload(
        self, sessions: async_sessionmaker
    ) -> None:
        """OAuthToken has only a relative expires_in; after a restart it is unusable."""
        storage = storage_for(sessions)
        await storage.set_tokens(a_token(expires_in=3600))

        fresh = storage_for(sessions)  # simulates a new process
        assert fresh.loaded_expiry_timestamp is None, "unknown until tokens are read"
        await fresh.get_tokens()
        assert fresh.loaded_expiry_timestamp is not None
        assert abs(fresh.loaded_expiry_timestamp - (time.time() + 3600)) < 5

    async def test_token_without_expiry_has_no_timestamp(
        self, sessions: async_sessionmaker
    ) -> None:
        storage = storage_for(sessions)
        await storage.set_tokens(a_token(expires_in=None))
        fresh = storage_for(sessions)
        await fresh.get_tokens()
        assert fresh.loaded_expiry_timestamp is None

    async def test_tokens_are_scoped_per_user(self, sessions: async_sessionmaker) -> None:
        await storage_for(sessions, USER).set_tokens(a_token(access_token="mine"))
        assert await storage_for(sessions, OTHER_USER).get_tokens() is None


class TestClientRegistrationIsShared:
    """get/set_client_info is the APP-WIDE DCR registration, not per-user state.

    Storing it per user would register a new OAuth client with Silpo for every
    Telegram user — hundreds of junk registrations, and likely a ban.
    """

    async def test_registration_written_by_one_user_is_seen_by_another(
        self, sessions: async_sessionmaker
    ) -> None:
        await storage_for(sessions, USER).set_client_info(
            OAuthClientInformationFull(client_id="shared-client", redirect_uris=[])
        )
        seen = await storage_for(sessions, OTHER_USER).get_client_info()
        assert seen is not None and seen.client_id == "shared-client"

    async def test_absent_before_registration(self, sessions: async_sessionmaker) -> None:
        assert await storage_for(sessions).get_client_info() is None


LOCAL = "http://localhost:8000/auth/silpo/callback"
TUNNEL = "https://komora.trycloudflare.com/auth/silpo/callback"


def registered(*uris: str) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id="c", redirect_uris=list(uris))  # type: ignore[arg-type]


class TestARegistrationBelongsToItsCallback:
    """The registration is made once and reused forever, and it carries the
    `redirect_uris` it was made with.

    Moving `KOMORA_PUBLIC_BASE_URL` — which the Mini App device test *requires*, since
    Telegram will not take a loopback web-app URL — left the stored client pointing at
    a callback this process no longer serves. Nothing noticed: the SDK would present a
    redirect_uri Silpo never registered, and the flow died with an OAuth error rather
    than anything Komora could explain.
    """

    async def test_a_registration_for_this_callback_is_reused(
        self, sessions: async_sessionmaker
    ) -> None:
        await storage_for(sessions).set_client_info(registered(LOCAL))
        seen = await storage_for(sessions, redirect_uri=LOCAL).get_client_info()
        assert seen is not None and seen.client_id == "c"

    async def test_a_trailing_slash_is_not_a_different_callback(
        self, sessions: async_sessionmaker
    ) -> None:
        await storage_for(sessions).set_client_info(registered(LOCAL + "/"))
        assert await storage_for(sessions, redirect_uri=LOCAL).get_client_info() is not None

    async def test_a_registration_for_another_callback_is_dropped(
        self, sessions: async_sessionmaker
    ) -> None:
        await storage_for(sessions).set_client_info(registered(LOCAL))
        assert await storage_for(sessions, redirect_uri=TUNNEL).get_client_info() is None

    async def test_dropping_it_clears_the_row_so_the_sdk_registers_afresh(
        self, sessions: async_sessionmaker
    ) -> None:
        """Returning None is not enough — the stale row would be read again next time."""
        await storage_for(sessions).set_client_info(registered(LOCAL))
        await storage_for(sessions, redirect_uri=TUNNEL).get_client_info()
        assert await OAuthClientRepo(sessions).get() is None

    async def test_one_of_several_callbacks_is_enough(self, sessions: async_sessionmaker) -> None:
        await storage_for(sessions).set_client_info(registered(LOCAL, TUNNEL))
        assert await storage_for(sessions, redirect_uri=TUNNEL).get_client_info() is not None

    async def test_no_declared_callback_disables_the_check(
        self, sessions: async_sessionmaker
    ) -> None:
        """The old behaviour, kept as the default: never guess a caller's callback."""
        await storage_for(sessions).set_client_info(registered(LOCAL))
        assert await storage_for(sessions).get_client_info() is not None


class TestExpiredTokenHandling:
    """Upstream bug #3250, open in mcp 2.0.0.

    `_initialize()` restores tokens but not `token_expiry_time`, and
    `is_token_valid()` reads `not self.token_expiry_time` — so a None expiry makes an
    expired token look valid. The stale token is sent, Silpo 401s, and the SDK runs a
    full interactive login instead of refreshing. We rebuild a provider per request,
    so users would be asked to re-link constantly.
    """

    @staticmethod
    async def _storage_with_expired_token(sessions: async_sessionmaker) -> DBTokenStorage:
        await storage_for(sessions).set_tokens(a_token(expires_in=-3600))
        storage = storage_for(sessions)
        await storage.get_tokens()
        return storage

    async def test_stock_provider_wrongly_reports_an_expired_token_as_valid(
        self, sessions: async_sessionmaker
    ) -> None:
        """Pins the upstream behaviour. If this ever fails, #3250 was fixed and
        PersistentOAuthClientProvider can be deleted."""
        storage = await self._storage_with_expired_token(sessions)
        provider = OAuthClientProvider(
            server_url=SERVER,
            client_metadata=build_client_metadata("https://komora.example"),
            storage=storage,
        )
        await provider._initialize()

        assert provider.context.token_expiry_time is None
        assert provider.context.is_token_valid() is True, "the bug we work around"

    async def test_our_provider_correctly_reports_it_as_expired(
        self, sessions: async_sessionmaker
    ) -> None:
        storage = await self._storage_with_expired_token(sessions)
        provider = PersistentOAuthClientProvider(
            server_url=SERVER,
            client_metadata=build_client_metadata("https://komora.example"),
            storage=storage,
        )
        await provider._initialize()

        assert provider.context.token_expiry_time is not None
        assert provider.context.is_token_valid() is False
        assert provider.context.can_refresh_token() is False, "no client_info registered yet"

    async def test_valid_token_stays_valid(self, sessions: async_sessionmaker) -> None:
        await storage_for(sessions).set_tokens(a_token(expires_in=3600))
        storage = storage_for(sessions)
        provider = PersistentOAuthClientProvider(
            server_url=SERVER,
            client_metadata=build_client_metadata("https://komora.example"),
            storage=storage,
        )
        await provider._initialize()
        assert provider.context.is_token_valid() is True


class TestClientMetadata:
    def test_public_https_callback_is_a_web_client(self) -> None:
        """The SDK defaults to `native`, meant for CLIs with loopback redirects.
        A strict authorization server may reject an https redirect_uri under it."""
        metadata = build_client_metadata("https://komora.example")
        assert metadata.application_type == "web"

    @pytest.mark.parametrize(
        "base", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"]
    )
    def test_loopback_callback_is_a_native_client(self, base: str) -> None:
        """A loopback redirect is `native` by definition (RFC 8252). Silpo accepts
        one, which is why local verification needs no tunnel."""
        assert build_client_metadata(base).application_type == "native"

    def test_loopback_redirect_uri_is_built_correctly(self) -> None:
        metadata = build_client_metadata("http://localhost:8000")
        assert str(metadata.redirect_uris[0]) == "http://localhost:8000/auth/silpo/callback"

    def test_redirect_uri_points_at_our_callback(self) -> None:
        metadata = build_client_metadata("https://komora.example")
        assert str(metadata.redirect_uris[0]) == "https://komora.example/auth/silpo/callback"

    def test_trailing_slash_does_not_double_up(self) -> None:
        metadata = build_client_metadata("https://komora.example/")
        assert str(metadata.redirect_uris[0]) == "https://komora.example/auth/silpo/callback"

    def test_requests_refresh_tokens(self) -> None:
        assert "refresh_token" in build_client_metadata("https://k.example").grant_types


class TestAuthorizationBridge:
    """The SDK generates `state` itself and `callback_handler()` takes no arguments,
    so `state` — recovered from the authorization URL — is the only thing that can
    route an incoming callback to the right waiting coroutine.
    """

    async def test_redirect_handler_sends_the_url_instead_of_opening_a_browser(self) -> None:
        bridge = AuthorizationBridge()
        sent: list[tuple[int, str]] = []

        async def send(telegram_id: int, url: str) -> None:
            sent.append((telegram_id, url))

        redirect, _ = bridge.handlers(USER, send)
        await redirect("https://mcp.silpo.ua/authorize?state=abc123&client_id=x")

        assert sent == [(USER, "https://mcp.silpo.ua/authorize?state=abc123&client_id=x")]
        assert bridge.pending_states() == ["abc123"]

    async def test_callback_resolves_the_waiting_handler(self) -> None:
        bridge = AuthorizationBridge()

        async def send(telegram_id: int, url: str) -> None:
            return None

        redirect, callback = bridge.handlers(USER, send)
        await redirect("https://s/authorize?state=st1")

        waiter = asyncio.create_task(callback())
        await asyncio.sleep(0)
        assert bridge.resolve("st1", code="the-code", iss="https://mcp.silpo.ua") is True

        result = await asyncio.wait_for(waiter, timeout=2)
        assert result.code == "the-code"
        assert result.state == "st1"
        assert result.iss == "https://mcp.silpo.ua", "iss must be forwarded (RFC 9207)"
        assert bridge.pending_states() == [], "resolved entries are cleaned up"

    async def test_unknown_state_is_rejected(self) -> None:
        assert AuthorizationBridge().resolve("never-seen", code="c") is False

    async def test_a_refused_send_files_no_flow(self) -> None:
        """`connect()` passes a `send_url` that refuses, and every message from an
        unlinked user goes through it. Registering before the send left one entry per
        message parked forever, since only `callback_handler` ever removes one.
        """
        bridge = AuthorizationBridge()

        async def refuse(telegram_id: int, url: str) -> None:
            raise NotAuthenticated("no tokens")

        redirect, _ = bridge.handlers(USER, refuse)
        for _ in range(3):
            with pytest.raises(NotAuthenticated):
                await redirect("https://s/authorize?state=st1")

        assert bridge.pending_states() == []

    async def test_flows_nobody_collects_are_pruned_once_they_expire(self) -> None:
        """A flow abandoned between the redirect and the wait has no other way out."""
        bridge = AuthorizationBridge(timeout_seconds=0.05)

        async def send(telegram_id: int, url: str) -> None:
            return None

        redirect, _ = bridge.handlers(USER, send)
        await redirect("https://s/authorize?state=abandoned")
        assert bridge.pending_states() == ["abandoned"]

        await asyncio.sleep(0.06)
        redirect_again, _ = bridge.handlers(USER + 1, send)
        await redirect_again("https://s/authorize?state=fresh")

        assert bridge.pending_states() == ["fresh"], "the stale flow is gone"

    async def test_authorization_url_without_state_is_rejected(self) -> None:
        bridge = AuthorizationBridge()

        async def send(telegram_id: int, url: str) -> None:
            return None

        redirect, _ = bridge.handlers(USER, send)
        with pytest.raises(ValueError, match="state"):
            await redirect("https://mcp.silpo.ua/authorize?client_id=x")

    async def test_waiting_times_out_and_cleans_up(self) -> None:
        bridge = AuthorizationBridge(timeout_seconds=0.05)

        async def send(telegram_id: int, url: str) -> None:
            return None

        redirect, callback = bridge.handlers(USER, send)
        await redirect("https://s/authorize?state=slow")

        with pytest.raises(TimeoutError):
            await callback()
        assert bridge.pending_states() == [], "a timed-out entry must not leak"

    async def test_concurrent_users_do_not_cross_wires(self) -> None:
        """Two people linking at once must each get their own code."""
        bridge = AuthorizationBridge()

        async def send(telegram_id: int, url: str) -> None:
            return None

        redirect_a, callback_a = bridge.handlers(USER, send)
        redirect_b, callback_b = bridge.handlers(OTHER_USER, send)
        await redirect_a("https://s/authorize?state=state-a")
        await redirect_b("https://s/authorize?state=state-b")

        task_a = asyncio.create_task(callback_a())
        task_b = asyncio.create_task(callback_b())
        await asyncio.sleep(0)

        bridge.resolve("state-b", code="code-b")
        bridge.resolve("state-a", code="code-a")

        assert (await asyncio.wait_for(task_a, 2)).code == "code-a"
        assert (await asyncio.wait_for(task_b, 2)).code == "code-b"
