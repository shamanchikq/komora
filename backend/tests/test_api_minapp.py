"""The Mini App surface over HTTP: handlers' Outcomes as JSON, behind initData auth.

The bot's guarantees must survive the second surface, so what is asserted here is not
the plumbing but the rules: identity is checked on every route, nothing is confirmed
without the second tap, and a basket id from another user buys nothing.
"""

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from komora.api.app import create_app
from komora.bot.handlers import MAX_TEXT
from komora.core.agent.tools import PROPOSE_BASKET
from komora.core.llm.protocol import LLMResponse, ToolCall
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


class TestPositionsComeFromTheClient:
    """A position is an index the caller chose, so it is checked like one.

    The routes disagreed: `qty` and `swap` bounds-checked, `remove` handed the number
    straight to SQL. SQLite reads a negative OFFSET as zero and returns the FIRST row,
    so `lines/-1/remove` deleted a line the request never named and answered 200.
    Postgres raises on the same query, so the two stores did not even agree on how to
    be wrong.
    """

    @pytest.mark.parametrize("position", [-1, -5])
    async def test_a_negative_position_removes_nothing(self, api, position: int) -> None:
        client, _ = api
        body = await _draft(client)
        basket_id = body["basket_id"]
        before = [line["name"] for line in body["cart"]["lines"]]

        response = await client.post(
            f"/api/baskets/{basket_id}/lines/{position}/remove", headers=header(USER)
        )
        assert response.json()["toast"] == "Ця позиція недоступна"

        after = await client.get(f"/api/baskets/{basket_id}", headers=header(USER))
        assert [line["name"] for line in after.json()["cart"]["lines"]] == before

    @pytest.mark.parametrize(
        "path,body", [("/lines/-1/qty", {"qty": 2}), ("/swap", {"position": -1})]
    )
    async def test_the_other_routes_refuse_it_too(self, api, path: str, body: dict) -> None:
        client, _ = api
        basket_id = (await _draft(client))["basket_id"]
        response = await client.post(
            f"/api/baskets/{basket_id}{path}", json=body, headers=header(USER)
        )
        assert response.json()["toast"] == "Ця позиція недоступна"


class TestBasketIdsComeFromTheClientToo:
    """The same lesson as the position bounds, one level up.

    FastAPI's `int` path type has no ceiling, so an id past what a row can hold reached
    the driver instead of the ownership gate: SQLite raised `OverflowError` and
    Postgres a bigint `DataError`, which is an unhandled 500 rather than a refusal.
    """

    HUGE = 10**26

    @pytest.mark.parametrize("path", ["/preview", "/push", "/trim", "/cancel"])
    async def test_an_unholdable_id_is_refused_not_crashed(self, api, path: str) -> None:
        client, _ = api
        response = await client.post(f"/api/baskets/{self.HUGE}{path}", headers=header(USER))
        assert response.status_code == 200
        assert response.json()["toast"] == "Ця чернетка недоступна"

    async def test_opening_one_is_refused_too(self, api) -> None:
        """`GET` is where a deep link lands, so it is the reachable one."""
        client, _ = api
        response = await client.get(f"/api/baskets/{self.HUGE}", headers=header(USER))
        assert response.status_code == 200
        assert response.json()["toast"] == "Ця чернетка недоступна"

    async def test_the_line_routes_refuse_it_before_the_position(self, api) -> None:
        client, _ = api
        response = await client.post(
            f"/api/baskets/{self.HUGE}/lines/0/qty", json={"qty": 2}, headers=header(USER)
        )
        assert response.json()["toast"] == "Ця чернетка недоступна"

    async def test_a_real_basket_still_opens(self, api) -> None:
        """The guard must not have eaten the ordinary case."""
        client, _ = api
        basket_id = (await _draft(client))["basket_id"]
        response = await client.get(f"/api/baskets/{basket_id}", headers=header(USER))
        assert response.json()["kind"] == "draft"


class TestTheStepperRoundsWhereResolveRounds:
    """`on_set_qty` said it rounded like `clamp_quantity` and only capped at stock."""

    async def test_a_countable_line_never_holds_a_fraction(self, api) -> None:
        client, _ = api
        basket_id = (await _draft(client))["basket_id"]
        response = await client.post(
            f"/api/baskets/{basket_id}/lines/0/qty", json={"qty": 2.5}, headers=header(USER)
        )
        # Silpo counts packs of milk, and 2,5 of them is not an order anyone can fill.
        # Erring downwards is the rule `resolve` already follows: one short is a
        # message, one over is money.
        assert response.json()["cart"]["lines"][0]["qty"] == 2

    async def test_a_weighted_line_lands_on_its_own_step(self, sessions) -> None:
        cheese = product("Пармезан 36 міс.", 999.00, step=0.1)
        cheese["weighted"] = True
        call = ToolCall(
            PROPOSE_BASKET,
            {
                "title": "Паста",
                "lines": [{"description": "пармезан", "quantity": 1, "reason_text": "просили"}],
            },
        )
        services, _, _ = services_for(
            sessions,
            llm=ScriptedLLM(LLMResponse(tool_calls=(call,))),
            mcp=FakeSilpo({"пармезан": [cheese]}),
        )
        async with AsyncClient(transport=_app(services), base_url="http://test") as client:
            basket_id = (await _draft(client, "треба пармезан"))["basket_id"]
            response = await client.post(
                f"/api/baskets/{basket_id}/lines/0/qty",
                json={"qty": 0.37},
                headers=header(USER),
            )

        line = response.json()["cart"]["lines"][0]
        assert line["weighted"] is True
        # 0,37 кг is not a weight Silpo sells; 0,4 is. Off the grid it would have been
        # refused at push, after the confirmation sheet had already promised it.
        assert line["qty"] == pytest.approx(0.4)


TRIMMABLE = ToolCall(
    PROPOSE_BASKET,
    {
        "title": "Кошик на тиждень",
        "lines": [
            {"description": "молоко", "quantity": 1, "reason_text": "просили"},
            {"description": "печиво", "quantity": 1, "reason_text": "до чаю", "optional": True},
            {"description": "хліб", "quantity": 1, "reason_text": "просили"},
            {"description": "цукерки", "quantity": 1, "reason_text": "до чаю", "optional": True},
        ],
    },
)


class TestTrimmingTheOptionalLines:
    """The one new route with no positive test, and the only one that removes several
    lines per call — where an off-by-one takes out the wrong ones."""

    @pytest.fixture
    async def stocked(self, sessions):  # type: ignore[no-untyped-def]
        catalogue = {
            **CATALOGUE,
            "печиво": [product("Печиво Марія", 31.00)],
            "цукерки": [product("Цукерки Рошен", 88.00)],
        }
        services, silpo, _ = services_for(
            sessions,
            llm=ScriptedLLM(LLMResponse(tool_calls=(TRIMMABLE,))),
            mcp=FakeSilpo(catalogue),
        )
        async with AsyncClient(transport=_app(services), base_url="http://test") as client:
            yield client, silpo

    async def test_it_drops_every_optional_line_and_keeps_the_rest(self, stocked) -> None:
        client, _ = stocked
        body = await _draft(client, "збери кошик")
        assert [line["optional"] for line in body["cart"]["lines"]] == [
            False,
            True,
            False,
            True,
        ], "interleaved on purpose, or reverse-order removal proves nothing"

        response = await client.post(f"/api/baskets/{body['basket_id']}/trim", headers=header(USER))
        cart = response.json()["cart"]
        # Removing 1 then 3 in ascending order would take out «печиво» and then
        # whatever slid up into index 3 — which is nothing at all.
        assert [line["name"] for line in cart["lines"]] == [
            "Молоко Яготинське 2,6%",
            "Хліб Київський",
        ]

    async def test_the_total_follows_the_lines_out(self, stocked) -> None:
        client, _ = stocked
        body = await _draft(client, "збери кошик")
        response = await client.post(f"/api/baskets/{body['basket_id']}/trim", headers=header(USER))
        assert Decimal(response.json()["cart"]["total"]) == Decimal("42.90") + Decimal("28.50")

    async def test_what_is_left_is_what_gets_pushed(self, stocked) -> None:
        """The draft is the contract: a trim has to reach Silpo, not just the screen."""
        client, _ = stocked
        basket_id = (await _draft(client, "збери кошик"))["basket_id"]
        await client.post(f"/api/baskets/{basket_id}/trim", headers=header(USER))
        await client.post(f"/api/baskets/{basket_id}/preview", headers=header(USER))
        report = await client.post(f"/api/baskets/{basket_id}/push", headers=header(USER))
        assert report.json()["report"]["added"] == [
            "Молоко Яготинське 2,6%",
            "Хліб Київський",
        ]

    async def test_a_basket_with_nothing_optional_says_so_and_changes_nothing(self, api) -> None:
        client, _ = api
        body = await _draft(client)
        response = await client.post(f"/api/baskets/{body['basket_id']}/trim", headers=header(USER))
        assert response.json()["kind"] == "spoke"

        after = await client.get(f"/api/baskets/{body['basket_id']}", headers=header(USER))
        assert len(after.json()["cart"]["lines"]) == len(body["cart"]["lines"])


class TestTheBudgetWarningFollowsTheCart:
    """It is a fact about total-against-cap, and every edit route moves the total.

    Stored once by the pipeline and never recomputed, it outlived the edit that fixed
    it: «Понад тижневий бюджет на 84,30 ₴» sat directly above a bar reading
    «лишається 1,50 ₴» — same screen, same cart. «Прибрати необовʼязкові» exists to
    cause exactly that transition, so it was the ordinary case, not an edge one.
    """

    @staticmethod
    async def _capped(sessions, cap: int):  # type: ignore[no-untyped-def]
        services, _, _ = services_for(sessions)
        await services.users.ensure(USER)
        await services.users.set_budget(USER, cap)
        return services

    async def test_an_edit_under_the_cap_clears_the_warning(self, sessions) -> None:
        services = await self._capped(sessions, 60)
        async with AsyncClient(transport=_app(services), base_url="http://test") as client:
            body = await _draft(client)
            assert [w for w in body["cart"]["warnings"] if w.startswith("over_budget:")]

            response = await client.post(
                f"/api/baskets/{body['basket_id']}/lines/0/remove", headers=header(USER)
            )

        cart = response.json()["cart"]
        assert Decimal(cart["total"]) <= 60
        assert [w for w in cart["warnings"] if w.startswith("over_budget:")] == []

    async def test_a_warning_that_still_holds_is_restated_at_the_new_figure(self, sessions) -> None:
        services = await self._capped(sessions, 10)
        async with AsyncClient(transport=_app(services), base_url="http://test") as client:
            body = await _draft(client)
            response = await client.post(
                f"/api/baskets/{body['basket_id']}/lines/0/remove", headers=header(USER)
            )

        cart = response.json()["cart"]
        overage = next(w for w in cart["warnings"] if w.startswith("over_budget:"))
        assert Decimal(overage.split(":", 1)[1]) == Decimal(cart["total"]) - 10

    async def test_reopening_a_basket_measures_it_afresh(self, sessions) -> None:
        """A deep link recomputes nothing — but a warning is derived, not stored."""
        services = await self._capped(sessions, 60)
        async with AsyncClient(transport=_app(services), base_url="http://test") as client:
            basket_id = (await _draft(client))["basket_id"]
            await client.post(f"/api/baskets/{basket_id}/lines/0/remove", headers=header(USER))
            reopened = await client.get(f"/api/baskets/{basket_id}", headers=header(USER))

        body = reopened.json()
        assert body["budget_cap"] == 60
        assert [w for w in body["cart"]["warnings"] if w.startswith("over_budget:")] == []


REMOVAL_ONLY = ToolCall(
    PROPOSE_BASKET,
    {"title": "Прибрати хліб", "lines": [], "removals": ["хліб"]},
)


class TestAConfirmationMustAskForSomething:
    async def test_a_removal_the_user_already_made_is_not_a_sheet(self, sessions) -> None:
        """Between the two taps the user can do the job themselves.

        «Прибери хліб» is a whole basket — nothing to add, one thing to take out. Take
        that thing out by hand in the Silpo app before confirming and the sheet has
        nothing left to ask: it counted to zero and offered «Додати 0 позицій», over a
        button whose label said the same.
        """
        silpo = FakeSilpo(CATALOGUE)
        services, _, _ = services_for(sessions, mcp=silpo)

        async with AsyncClient(transport=_app(services), base_url="http://test") as client:
            # A basket Komora synced, so «хліб» is a removal candidate at all.
            first = await _draft(client)
            await client.post(f"/api/baskets/{first['basket_id']}/preview", headers=header(USER))
            await client.post(f"/api/baskets/{first['basket_id']}/push", headers=header(USER))

            services.llm._responses.append(LLMResponse(tool_calls=(REMOVAL_ONLY,)))
            second = await _draft(client, "прибери хліб")
            assert [r["name"] for r in second["cart"]["removals"]] == ["Хліб Київський"]

            # …and now the user does it themselves, in the Silpo app.
            await silpo.remove_cart_products("cart", [{"productId": "id-Хліб Київський"}])
            silpo.remove_calls.clear()

            response = await client.post(
                f"/api/baskets/{second['basket_id']}/preview", headers=header(USER)
            )

        body = response.json()
        assert body["kind"] == "spoke", "no sheet at all, rather than one asking for nothing"
        assert "нема чого міняти" in body["text"]
        assert silpo.remove_calls == []


class TestTheTextATurnMayCarry:
    """The bot gets this ceiling from Telegram, which will not deliver a longer
    message. HTTP inherits nothing, so an unbounded body went into the conversation
    table and into the model prompt."""

    async def test_a_body_longer_than_a_telegram_message_is_refused(self, api) -> None:
        client, _ = api
        response = await client.post(
            "/api/draft", json={"text": "молоко " * MAX_TEXT}, headers=header(USER)
        )
        body = response.json()
        assert body["kind"] == "spoke"
        assert "коротше" in body["text"]

    async def test_it_is_refused_before_anything_is_written(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        async with AsyncClient(transport=_app(services), base_url="http://test") as client:
            await client.post(
                "/api/draft", json={"text": "молоко " * MAX_TEXT}, headers=header(USER)
            )
        assert await services.conversations.last_n(USER) == []

    async def test_a_message_at_the_limit_still_builds_a_basket(self, api) -> None:
        client, _ = api
        response = await client.post(
            "/api/draft", json={"text": "молоко".ljust(MAX_TEXT)}, headers=header(USER)
        )
        assert response.json()["kind"] == "draft"


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
