"""The Mini App surface over HTTP: handlers' Outcomes as JSON, behind initData auth.

The bot's guarantees must survive the second surface, so what is asserted here is not
the plumbing but the rules: identity is checked on every route, nothing is confirmed
without the second tap, and a basket id from another user buys nothing.
"""

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from komora.api.app import create_app
from komora.core.llm.protocol import LLMResponse
from komora.core.mcp.auth import AuthorizationBridge
from tests.fakes import INITDATA_TOKEN, FakeSilpo, product, signed_init_data
from tests.test_handlers import CATALOGUE, USER, ScriptedLLM, services_for

OTHER = 777


def header(user_id: int, **kw: int) -> dict[str, str]:
    return {"Authorization": f"tma {signed_init_data(user_id, **kw)}"}


def _app(services) -> ASGITransport:  # type: ignore[no-untyped-def]
    return ASGITransport(app=create_app(AuthorizationBridge(), services, INITDATA_TOKEN))


@pytest.fixture
async def api(sessions):  # type: ignore[no-untyped-def]
    services, silpo, _ = services_for(sessions)
    async with AsyncClient(transport=_app(services), base_url="http://test") as client:
        yield client, silpo


async def _draft(client, text: str = "купи молоко і хліб", user_id: int = USER) -> dict:
    response = await client.post("/api/draft", json={"text": text}, headers=header(user_id))
    assert response.status_code == 200
    return response.json()


class TestAuthGate:
    async def test_no_header_is_unauthorized(self, api) -> None:
        client, _ = api
        response = await client.post("/api/draft", json={"text": "купи молоко"})
        assert response.status_code == 401

    async def test_a_wrong_scheme_is_unauthorized(self, api) -> None:
        client, _ = api
        response = await client.post(
            "/api/draft",
            json={"text": "купи молоко"},
            headers={"Authorization": "Bearer whatever"},
        )
        assert response.status_code == 401

    async def test_a_forged_signature_is_unauthorized(self, api) -> None:
        """The whole point of the check: a guessed payload is not an identity."""
        client, _ = api
        payload = signed_init_data(USER)
        forged = {"Authorization": f"tma {payload[:-2]}xy"}
        response = await client.post("/api/draft", json={"text": "купи молоко"}, headers=forged)
        assert response.status_code == 401

    async def test_a_stale_launch_is_unauthorized(self, api) -> None:
        client, _ = api
        response = await client.post(
            "/api/draft",
            json={"text": "купи молоко"},
            headers=header(USER, age_s=86_401),
        )
        assert response.status_code == 401


class TestDraftFlow:
    async def test_text_becomes_a_reviewable_draft(self, api) -> None:
        client, _ = api
        body = await _draft(client)
        assert body["kind"] == "draft"
        assert body["basket_id"] is not None
        assert [line["name"] for line in body["cart"]["lines"]] == [
            "Молоко Яготинське 2,6%",
            "Хліб Київський",
        ]
        assert all(line["reason_text"] for line in body["cart"]["lines"])
        assert isinstance(body["cart"]["total"], str), "money crosses as text, not float"

    async def test_every_line_carries_its_reason(self, api) -> None:
        client, _ = api
        body = await _draft(client)
        assert {line["reason_kind"] for line in body["cart"]["lines"]} == {"stated"}

    async def test_a_free_form_question_is_spoken_not_carted(self, sessions) -> None:
        llm = ScriptedLLM(LLMResponse(text="Є грузинське вино за 420 ₴"))
        services, _, _ = services_for(sessions, llm=llm)
        async with AsyncClient(transport=_app(services), base_url="http://test") as client:
            body = await _draft(client, text="яке вино є до 500?")
        assert body["kind"] == "spoke"
        assert "вино" in body["text"]
        assert body["needs_link"] is False


class TestConfirmationRules:
    async def test_the_two_taps_in_order(self, api) -> None:
        """Nothing reaches Silpo without preview then push — over HTTP as in chat."""
        client, silpo = api
        basket_id = (await _draft(client))["basket_id"]
        assert silpo.add_calls == [], "a draft alone writes nothing"

        preview = (
            await client.post(f"/api/baskets/{basket_id}/preview", headers=header(USER))
        ).json()
        assert preview["kind"] == "preview"
        assert preview["preview"]["adding_count"] == 2
        assert silpo.add_calls == [], "a preview still writes nothing"

        pushed = (await client.post(f"/api/baskets/{basket_id}/push", headers=header(USER))).json()
        assert pushed["kind"] == "synced"
        assert pushed["report"]["ok"] is True
        assert len(silpo.add_calls[-1]) == 2, "both lines landed"

    async def test_a_second_push_is_refused_after_success(self, api) -> None:
        client, silpo = api
        basket_id = (await _draft(client))["basket_id"]
        await client.post(f"/api/baskets/{basket_id}/preview", headers=header(USER))
        await client.post(f"/api/baskets/{basket_id}/push", headers=header(USER))

        replayed = (
            await client.post(f"/api/baskets/{basket_id}/push", headers=header(USER))
        ).json()
        assert replayed["kind"] == "spoke"
        assert replayed["toast"] == "Чернетка вже неактуальна"
        assert len(silpo.add_calls) == 1, "a replayed id must not write again"

    async def test_a_foreign_user_gets_nowhere(self, api) -> None:
        """The ownership check the bot applies to callbacks, re-applied to HTTP."""
        client, silpo = api
        basket_id = (await _draft(client))["basket_id"]

        for action in ("preview", "push", "cancel"):
            response = (
                await client.post(f"/api/baskets/{basket_id}/{action}", headers=header(OTHER))
            ).json()
            assert response["kind"] == "spoke"
            assert response["toast"] == "Ця чернетка недоступна"
        assert silpo.add_calls == []

    async def test_cancel_discards_so_push_refuses(self, api) -> None:
        client, silpo = api
        basket_id = (await _draft(client))["basket_id"]
        cancelled = (
            await client.post(f"/api/baskets/{basket_id}/cancel", headers=header(USER))
        ).json()
        assert cancelled["kind"] == "spoke"

        refused = (await client.post(f"/api/baskets/{basket_id}/push", headers=header(USER))).json()
        assert refused["toast"] == "Чернетка вже неактуальна"
        assert silpo.add_calls == []


class TestLineEdits:
    """The stepper and ✕ act on the *persisted* draft — what push later sends."""

    async def test_a_stepper_tap_persists_and_recomputes(self, api) -> None:
        client, silpo = api
        basket_id = (await _draft(client))["basket_id"]

        response = await client.post(
            f"/api/baskets/{basket_id}/lines/0/qty", json={"qty": 5}, headers=header(USER)
        )
        body = response.json()
        assert body["kind"] == "draft"
        assert body["cart"]["lines"][0]["qty"] == 5
        assert body["cart"]["total"] == "243.000", "5 × 42,90 + хліб 28,50"

        await client.post(f"/api/baskets/{basket_id}/preview", headers=header(USER))
        await client.post(f"/api/baskets/{basket_id}/push", headers=header(USER))
        last = silpo.add_calls[-1]
        assert {item["productId"]: item["quantity"] for item in last} == {
            "id-Молоко Яготинське 2,6%": 5,
            "id-Хліб Київський": 1,
        }

    async def test_a_quantity_over_stock_is_capped(self, api) -> None:
        """`stock` ships on the line so the ceiling is enforced where the number lands."""
        client, _ = api
        basket_id = (await _draft(client))["basket_id"]
        response = await client.post(
            f"/api/baskets/{basket_id}/lines/0/qty", json={"qty": 99}, headers=header(USER)
        )
        assert response.json()["cart"]["lines"][0]["qty"] == 10

    async def test_a_quantity_that_is_not_a_number_never_reaches_the_draft(self, api) -> None:
        """`NaN` passes `round`, passes `<= 0`, and `min(nan, stock)` is `nan` — the
        first thing to refuse it was the NOT NULL column, as an unhandled 500. JSON
        carries the literal whether or not a stepper ever sends one."""
        client, _ = api
        basket_id = (await _draft(client))["basket_id"]
        response = await client.post(
            f"/api/baskets/{basket_id}/lines/0/qty",
            content=b'{"qty": NaN}',
            headers={**header(USER), "Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["toast"] == "Кількість має бути більша за нуль"

        after = await client.post(f"/api/baskets/{basket_id}/preview", headers=header(USER))
        assert after.json()["kind"] == "preview", "the draft is untouched and still usable"
        assert after.json()["preview"]["adding_count"] == 2

    async def test_a_non_positive_quantity_is_answered_in_words(self, api) -> None:
        """Not a 422: a stepper cannot send this, but the refusal a person might see
        belongs in Ukrainian rather than in a validation envelope."""
        client, _ = api
        basket_id = (await _draft(client))["basket_id"]
        response = await client.post(
            f"/api/baskets/{basket_id}/lines/0/qty", json={"qty": 0}, headers=header(USER)
        )
        assert response.status_code == 200
        assert response.json()["toast"] == "Кількість має бути більша за нуль"

    async def test_removing_a_line_takes_it_out_of_the_push(self, api) -> None:
        client, silpo = api
        basket_id = (await _draft(client))["basket_id"]
        response = await client.post(
            f"/api/baskets/{basket_id}/lines/1/remove", headers=header(USER)
        )
        body = response.json()
        assert [line["name"] for line in body["cart"]["lines"]] == ["Молоко Яготинське 2,6%"]

        await client.post(f"/api/baskets/{basket_id}/preview", headers=header(USER))
        await client.post(f"/api/baskets/{basket_id}/push", headers=header(USER))
        assert len(silpo.add_calls[-1]) == 1

    async def test_line_edits_belong_to_the_owner(self, api) -> None:
        client, silpo = api
        basket_id = (await _draft(client))["basket_id"]
        for path, body in (
            ("/lines/0/qty", {"qty": 3}),
            ("/lines/0/remove", None),
            ("/trim", None),
        ):
            response = await client.post(
                f"/api/baskets/{basket_id}{path}", json=body, headers=header(OTHER)
            )
            assert response.json()["toast"] == "Ця чернетка недоступна"
        assert silpo.add_calls == []


_MONEY = {"unit_price", "old_price", "line_total"}


def _amounts(cart: dict) -> list[dict]:
    """Lines with their money read as amounts rather than as text.

    Scale is not stable across storage: Silpo quotes «42.9», a `Numeric(10, 2)` column
    gives it back as «42.90», and the computed `line_total` inherits the difference.
    The same cart is then two different JSON documents — which no formatter on either
    surface can tell apart, and which is not what these tests are about.
    """
    return [
        {k: Decimal(v) if k in _MONEY and v is not None else v for k, v in line.items()}
        for line in cart["lines"]
    ]


class TestOpeningABasket:
    """Where a deep link lands. The id travels inside the signed launch payload, which
    proves what the *link* said and nothing about who may act on that basket."""

    async def test_the_owner_sees_the_basket_a_link_named(self, api) -> None:
        client, _ = api
        drafted = await _draft(client)
        response = await client.get(f"/api/baskets/{drafted['basket_id']}", headers=header(USER))
        assert response.status_code == 200
        assert response.json()["kind"] == "draft"

        opened = response.json()["cart"]
        assert _amounts(opened) == _amounts(drafted["cart"]), "opening changes nothing"
        assert Decimal(opened["total"]) == Decimal(drafted["cart"]["total"])
        assert Decimal(opened["estimated_savings"]) == Decimal(drafted["cart"]["estimated_savings"])

    async def test_a_guessed_id_buys_nothing(self, api) -> None:
        client, _ = api
        basket_id = (await _draft(client))["basket_id"]
        response = await client.get(f"/api/baskets/{basket_id}", headers=header(OTHER))
        assert response.json()["kind"] == "spoke"
        assert response.json()["toast"] == "Ця чернетка недоступна"

    async def test_a_link_to_a_basket_already_sent_is_refused(self, api) -> None:
        """A link lives in a chat message for as long as the chat does."""
        client, _ = api
        basket_id = (await _draft(client))["basket_id"]
        await client.post(f"/api/baskets/{basket_id}/preview", headers=header(USER))
        await client.post(f"/api/baskets/{basket_id}/push", headers=header(USER))

        response = await client.get(f"/api/baskets/{basket_id}", headers=header(USER))
        assert response.json()["toast"] == "Чернетка вже неактуальна"

    async def test_a_link_to_a_discarded_basket_is_refused(self, api) -> None:
        client, _ = api
        basket_id = (await _draft(client))["basket_id"]
        await client.post(f"/api/baskets/{basket_id}/cancel", headers=header(USER))
        assert (await client.get(f"/api/baskets/{basket_id}", headers=header(USER))).json()[
            "kind"
        ] == "spoke"

    async def test_opening_still_needs_an_identity(self, api) -> None:
        client, _ = api
        basket_id = (await _draft(client))["basket_id"]
        assert (await client.get(f"/api/baskets/{basket_id}")).status_code == 401


class TestSwapRoute:
    async def test_a_swap_returns_an_updated_draft(self, sessions) -> None:
        alternative = product("Молоко Злагода 2,5%", 39.90)
        mcp = FakeSilpo(
            {
                "молоко": [*CATALOGUE["молоко"], alternative],
                "хліб": CATALOGUE["хліб"],
            }
        )
        services, _, _ = services_for(sessions, mcp=mcp)
        async with AsyncClient(transport=_app(services), base_url="http://test") as client:
            basket_id = (await _draft(client))["basket_id"]
            response = await client.post(
                f"/api/baskets/{basket_id}/swap",
                json={"position": 0},
                headers=header(USER),
            )

        body = response.json()
        assert body["kind"] == "draft"
        assert body["cart"]["lines"][0]["name"] == "Молоко Злагода 2,5%"
        assert body["toast"] is not None and "Злагода" in body["toast"]

    async def test_a_swap_on_someone_else_s_basket_is_refused(self, sessions) -> None:
        mcp = FakeSilpo({"молоко": [*CATALOGUE["молоко"], product("Молоко Злагода 2,5%", 39.90)]})
        services, _, _ = services_for(sessions, mcp=mcp)
        async with AsyncClient(transport=_app(services), base_url="http://test") as client:
            basket_id = (await _draft(client))["basket_id"]
            response = await client.post(
                f"/api/baskets/{basket_id}/swap",
                json={"position": 0},
                headers=header(OTHER),
            )

        body = response.json()
        assert body["kind"] == "spoke"
        assert body["toast"] == "Ця чернетка недоступна"


def test_the_built_app_is_served_same_origin() -> None:
    """`web/dist` mounts at `/` when it exists — same origin, so the Mini App's
    fetches carry no CORS. Skipped where the frontend was never built."""
    import pytest
    from fastapi.testclient import TestClient

    from komora.api.app import _WEB_DIST
    from tests.fakes import INITDATA_TOKEN as TOKEN

    if not _WEB_DIST.is_dir():
        pytest.skip("web/dist not built")
    with TestClient(create_app(AuthorizationBridge(), None, TOKEN)) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Комора" in response.text
