"""The OAuth callback endpoint.

Note there is deliberately no `/auth/silpo/start` endpoint. Account linking is
initiated by the bot, which already knows who the user is; the SDK then hands us the
authorization URL and we deliver it over Telegram. An unauthenticated HTTP endpoint
taking a telegram_id would let anyone start a flow for anyone.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from komora.api.app import create_app
from komora.core.mcp.auth import AuthorizationBridge

USER = 4242


@pytest.fixture
def bridge() -> AuthorizationBridge:
    return AuthorizationBridge()


@pytest.fixture
def client(bridge: AuthorizationBridge) -> TestClient:
    return TestClient(create_app(bridge))


async def _noop_send(telegram_id: int, url: str) -> None:
    return None


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200


class TestCallback:
    def test_unknown_state_is_rejected(self, client: TestClient) -> None:
        response = client.get("/auth/silpo/callback", params={"code": "c", "state": "nope"})
        assert response.status_code == 400

    def test_missing_code_is_rejected(self, client: TestClient) -> None:
        assert client.get("/auth/silpo/callback", params={"state": "s"}).status_code == 422

    async def test_successful_callback_hands_the_code_to_the_waiter(
        self, bridge: AuthorizationBridge
    ) -> None:
        redirect, callback = bridge.handlers(USER, _noop_send)
        await redirect("https://mcp.silpo.ua/authorize?state=st1")
        waiter = asyncio.create_task(callback())
        await asyncio.sleep(0)

        with TestClient(create_app(bridge)) as client:
            response = await asyncio.to_thread(
                client.get,
                "/auth/silpo/callback",
                params={"code": "the-code", "state": "st1", "iss": "https://mcp.silpo.ua"},
            )

        assert response.status_code == 200
        assert "Telegram" in response.text
        result = await asyncio.wait_for(waiter, timeout=2)
        assert result.code == "the-code"
        assert result.iss == "https://mcp.silpo.ua"

    async def test_denied_authorization_fails_the_waiter_promptly(
        self, bridge: AuthorizationBridge
    ) -> None:
        """If the user declines, the waiting coroutine must not hang until timeout."""
        redirect, callback = bridge.handlers(USER, _noop_send)
        await redirect("https://mcp.silpo.ua/authorize?state=st2")
        waiter = asyncio.create_task(callback())
        await asyncio.sleep(0)

        with TestClient(create_app(bridge)) as client:
            response = await asyncio.to_thread(
                client.get,
                "/auth/silpo/callback",
                params={"error": "access_denied", "state": "st2"},
            )

        assert response.status_code == 200, "the user sees an explanation, not a stack trace"
        with pytest.raises(PermissionError, match="access_denied"):
            await asyncio.wait_for(waiter, timeout=2)

    def test_error_without_a_known_state_is_rejected(self, client: TestClient) -> None:
        response = client.get(
            "/auth/silpo/callback", params={"error": "access_denied", "state": "unknown"}
        )
        assert response.status_code == 400

    def test_response_is_html_a_person_can_read(self, bridge: AuthorizationBridge) -> None:
        async def setup() -> None:
            redirect, _ = bridge.handlers(USER, _noop_send)
            await redirect("https://s/authorize?state=st3")

        asyncio.run(setup())
        with TestClient(create_app(bridge)) as client:
            response = client.get("/auth/silpo/callback", params={"code": "c", "state": "st3"})
        assert response.headers["content-type"].startswith("text/html")
