"""The conversation, end to end, with Telegram and the network removed.

These drive the handler functions directly against a real (in-memory) database, a
scripted LLM and the Silpo fake, so the whole loop — message, draft, preview, sync — is
exercised without an aiogram object anywhere.
"""

import contextlib
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from komora.bot.handlers import (
    NEED_AUTH,
    NO_CONTEXT,
    SILPO_DOWN,
    Services,
    on_budget,
    on_callback,
    on_start,
    on_text,
)
from komora.core.agent.tools import PROPOSE_BASKET
from komora.core.llm.protocol import LLMResponse, ToolCall
from komora.core.mcp.errors import McpUnavailable, NotAuthenticated
from komora.db.repo import BasketRepo, ConversationRepo, UserRepo
from tests.fakes import FakeSilpo, product

USER = 4242

BASKET_CALL = ToolCall(
    PROPOSE_BASKET,
    {
        "title": "Звичайний кошик",
        "lines": [
            {"description": "молоко", "quantity": 2, "reason_text": "ви попросили"},
            {"description": "хліб", "quantity": 1, "reason_text": "ви попросили"},
        ],
    },
)

CATALOGUE = {
    "молоко": [product("Молоко Яготинське 2,6%", 42.90)],
    "хліб": [product("Хліб Київський", 28.50)],
}


class ScriptedLLM:
    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, *, system, messages, tools=()):  # type: ignore[no-untyped-def]
        self.calls.append({"system": system, "messages": list(messages), "tools": list(tools)})
        return self._responses.pop(0) if self._responses else LLMResponse(text="ок")


def services_for(
    sessions: async_sessionmaker,
    *,
    llm: ScriptedLLM | None = None,
    mcp: FakeSilpo | None = None,
    connect_error: Exception | None = None,
    verify: bool = False,
) -> tuple[Services, FakeSilpo, list[int]]:
    silpo = mcp or FakeSilpo(CATALOGUE)
    linked: list[int] = []

    @contextlib.asynccontextmanager
    async def connect(telegram_id: int) -> AsyncIterator[FakeSilpo]:
        if connect_error is not None:
            raise connect_error
        yield silpo

    async def start_linking(telegram_id: int) -> None:
        linked.append(telegram_id)

    async def no_tools(mcp: FakeSilpo) -> list:
        return []

    return (
        Services(
            users=UserRepo(sessions),
            conversations=ConversationRepo(sessions),
            baskets=BasketRepo(sessions),
            llm=llm or ScriptedLLM(LLMResponse(tool_calls=(BASKET_CALL,))),
            tools=no_tools,  # type: ignore[arg-type]
            connect=connect,  # type: ignore[arg-type]
            verify=verify,
            start_linking=start_linking,
        ),
        silpo,
        linked,
    )


def button_data(reply) -> list[str | None]:  # type: ignore[no-untyped-def]
    return [b.data for b in reply.buttons]


class TestStart:
    async def test_unlinked_user_is_told_why_access_is_needed(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        reply = await on_start(services, USER)
        assert "доступ до вашого акаунта Сільпо" in reply.text
        assert "нічого не додає" in reply.text, "the promise has to be made up front"
        assert button_data(reply) == ["link"]

    async def test_linked_user_is_ready_to_go(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        await services.users.set_token_blob(USER, b"encrypted", None)
        reply = await on_start(services, USER)
        assert "підключено" in reply.text.lower()
        assert reply.buttons == ()

    async def test_the_link_button_starts_the_flow(self, sessions) -> None:
        services, _, linked = services_for(sessions)
        await on_callback(services, USER, "link")
        assert linked == [USER]


class TestConversation:
    async def test_a_plain_question_is_answered_as_text(self, sessions) -> None:
        llm = ScriptedLLM(LLMResponse(text="Є грузинське вино за 420 ₴"))
        services, _, _ = services_for(sessions, llm=llm)
        reply = await on_text(services, USER, "яке грузинське вино є до 500 ₴?")
        assert reply.text == "Є грузинське вино за 420 ₴"
        assert reply.buttons == ()

    async def test_a_stated_list_becomes_a_reviewable_draft(self, sessions) -> None:
        services, silpo, _ = services_for(sessions)
        reply = await on_text(services, USER, "купи молоко і хліб")

        assert "Молоко Яготинське" in reply.text and "Хліб Київський" in reply.text
        assert reply.text.count("— ви попросили") == 2, "every line carries its reason"
        actions = [d and d.split(":")[0] for d in button_data(reply)]
        assert actions == ["swap", "swap", "sync", "cancel"], "one swap per line"
        assert silpo.add_calls == [], "nothing may reach the cart before confirmation"

    async def test_the_draft_is_persisted_for_the_confirm_tap(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        await on_text(services, USER, "купи молоко і хліб")
        basket = await services.baskets.get_active(USER)
        assert basket is not None and basket.status == "draft"
        cart = await services.baskets.load_cart(basket.id)
        assert cart is not None and len(cart.lines) == 2

    async def test_history_is_recorded_without_duplicating_the_new_message(self, sessions) -> None:
        llm = ScriptedLLM(LLMResponse(text="перша"), LLMResponse(text="друга"))
        services, _, _ = services_for(sessions, llm=llm)
        await on_text(services, USER, "привіт")
        await on_text(services, USER, "ще раз")

        second_prompt = [m.content for m in llm.calls[1]["messages"]]
        assert second_prompt == ["привіт", "перша", "ще раз"]

    async def test_a_budget_cap_is_applied_to_the_draft(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        await services.users.ensure(USER)
        await services.users.set_budget(USER, 50)
        reply = await on_text(services, USER, "купи молоко і хліб")
        assert "перевищено" in reply.text

    async def test_nothing_found_produces_no_send_button(self, sessions) -> None:
        services, _, _ = services_for(sessions, mcp=FakeSilpo({}))
        reply = await on_text(services, USER, "купи молоко і хліб")
        assert reply.buttons == ()
        assert "Не знайшлося" in reply.text


class TestFailurePaths:
    async def test_an_unlinked_account_is_offered_the_login(self, sessions) -> None:
        services, _, _ = services_for(sessions, connect_error=NotAuthenticated())
        reply = await on_text(services, USER, "купи молоко")
        assert reply.text == NEED_AUTH
        assert button_data(reply) == ["link"]

    async def test_a_cart_without_a_branch_explains_what_to_fix(self, sessions) -> None:
        """Silpo cannot search without a store and a timeslot, and only the user can
        choose them."""
        broken = FakeSilpo(CATALOGUE)
        original = broken.get_shopping_cart_by_id

        async def no_context(cart_id: str) -> dict:
            payload = await original(cart_id)
            payload["cart"]["shipments"] = []
            return payload

        broken.get_shopping_cart_by_id = no_context  # type: ignore[method-assign]
        services, _, _ = services_for(sessions, mcp=broken)
        reply = await on_text(services, USER, "купи молоко")
        assert reply.text == NO_CONTEXT

    async def test_silpo_being_down_is_reported_as_such(self, sessions) -> None:
        services, _, _ = services_for(sessions, connect_error=McpUnavailable("down"))
        assert (await on_text(services, USER, "купи молоко")).text == SILPO_DOWN


class TestConfirmation:
    async def _draft(self, sessions, **kw):  # type: ignore[no-untyped-def]
        services, silpo, _ = services_for(sessions, **kw)
        await on_text(services, USER, "купи молоко і хліб")
        basket = await services.baskets.get_active(USER)
        assert basket is not None
        return services, silpo, basket.id

    async def test_confirming_shows_a_preview_before_anything_is_sent(self, sessions) -> None:
        services, silpo, basket_id = await self._draft(sessions)
        reply = await on_callback(services, USER, f"sync:{basket_id}")
        assert "Надіслати в Сільпо?" in reply.text
        assert [d and d.split(":")[0] for d in button_data(reply)] == ["push", "cancel"]
        assert silpo.add_calls == [], "the preview must not write anything"

    async def test_the_preview_reports_what_is_already_in_the_silpo_cart(self, sessions) -> None:
        existing = [
            {
                "productId": "id-Морозиво",
                "companyId": "c",
                "branchId": "b",
                "name": "Морозиво",
                "price": 139,
                "quantity": 1,
            }
        ]
        services, _, basket_id = await self._draft(
            sessions, mcp=FakeSilpo(CATALOGUE, existing=existing)
        )
        reply = await on_callback(services, USER, f"sync:{basket_id}")
        assert "вже 1 позиція" in reply.text and "не чіпаємо" in reply.text

    async def test_pushing_adds_to_the_cart_and_offers_checkout(self, sessions) -> None:
        services, silpo, basket_id = await self._draft(sessions)
        await on_callback(services, USER, f"sync:{basket_id}")
        reply = await on_callback(services, USER, f"push:{basket_id}")

        assert "Готово" in reply.text
        assert [b.url for b in reply.buttons] == ["https://silpo.ua/checkout/abc"]
        sent = [p["productId"] for batch in silpo.add_calls for p in batch]
        assert sorted(sent) == ["id-Молоко Яготинське 2,6%", "id-Хліб Київський"]

    async def test_a_completed_sync_closes_the_draft(self, sessions) -> None:
        services, _, basket_id = await self._draft(sessions)
        await on_callback(services, USER, f"push:{basket_id}")
        assert await services.baskets.get_status(basket_id) == "synced"

    async def test_a_partial_failure_keeps_the_draft_open_for_a_retry(self, sessions) -> None:
        silpo = FakeSilpo(CATALOGUE, reject={"id-Хліб Київський"})
        services, _, basket_id = await self._draft(sessions, mcp=silpo)
        reply = await on_callback(services, USER, f"push:{basket_id}")

        assert "Додалося не все" in reply.text
        assert "Хліб Київський" in reply.text
        assert await services.baskets.get_status(basket_id) == "draft"
        assert "push" in str(button_data(reply)), "a retry has to be offered"

    async def test_cancelling_discards_the_draft_and_touches_nothing(self, sessions) -> None:
        services, silpo, basket_id = await self._draft(sessions)
        reply = await on_callback(services, USER, f"cancel:{basket_id}")
        assert "у кошику Сільпо нічого не змінилося" in reply.text
        assert await services.baskets.get_status(basket_id) == "discarded"
        assert silpo.add_calls == []

    async def test_a_stale_draft_cannot_be_synced_twice(self, sessions) -> None:
        services, silpo, basket_id = await self._draft(sessions)
        await on_callback(services, USER, f"push:{basket_id}")
        before = len(silpo.add_calls)
        reply = await on_callback(services, USER, f"push:{basket_id}")
        assert reply.toast is not None
        assert len(silpo.add_calls) == before, "a second tap must not re-send"


class TestOwnership:
    async def test_a_basket_belonging_to_someone_else_is_refused(self, sessions) -> None:
        """The id arrives from the client, so ownership is checked, not assumed."""
        services, silpo, _ = services_for(sessions)
        await on_text(services, USER, "купи молоко і хліб")
        basket = await services.baskets.get_active(USER)
        assert basket is not None

        intruder = 9999
        reply = await on_callback(services, intruder, f"push:{basket.id}")
        assert reply.toast == "Ця чернетка недоступна"
        assert silpo.add_calls == []
        assert await services.baskets.get_status(basket.id) == "draft"

    async def test_an_unknown_basket_is_refused(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        assert (await on_callback(services, USER, "push:12345")).toast is not None

    @pytest.mark.parametrize("data", ["push:абв", "нісенітниця", "sync:"])
    async def test_malformed_callback_data_is_survivable(self, sessions, data: str) -> None:
        services, _, _ = services_for(sessions)
        assert (await on_callback(services, USER, data)).text


class TestBudgetCommand:
    async def test_setting_and_clearing(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        assert "1500" in (await on_budget(services, USER, "1500")).text
        user = await services.users.get(USER)
        assert user is not None and user.budget_weekly == 1500

        await on_budget(services, USER, "0")
        user = await services.users.get(USER)
        assert user is not None and user.budget_weekly is None

    async def test_showing_the_current_value(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        await on_budget(services, USER, "800")
        assert "800 ₴" in (await on_budget(services, USER, "")).text

    async def test_nonsense_gets_the_help_text(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        assert "/budget" in (await on_budget(services, USER, "багато")).text


async def test_the_full_happy_path(sessions) -> None:
    """message -> draft -> preview -> add, with the user's own cart left intact."""
    existing = [
        {
            "productId": "id-Морозиво",
            "companyId": "c",
            "branchId": "b",
            "name": "Морозиво",
            "price": 139,
            "quantity": 1,
        }
    ]
    silpo = FakeSilpo(CATALOGUE, existing=existing)
    services, _, _ = services_for(sessions, mcp=silpo)

    await on_start(services, USER)
    draft = await on_text(services, USER, "купи молоко і хліб")
    basket_id = int(str(draft.buttons[0].data).split(":")[1])

    preview = await on_callback(services, USER, f"sync:{basket_id}")
    assert "не чіпаємо" in preview.text

    report = await on_callback(services, USER, f"push:{basket_id}")
    assert "Готово" in report.text

    cart = (await silpo.get_shopping_cart_by_id("x"))["cart"]
    names = {p["name"] for p in cart["shipments"][0]["products"]}
    assert "Морозиво" in names, "the user's own item survived"
    assert len(names) == 3

    total = sum(
        Decimal(str(p["price"])) * Decimal(str(p["quantity"]))
        for p in cart["shipments"][0]["products"]
    )
    assert total == Decimal("253.30")


class TestSwap:
    """«Інший варіант» re-runs the line's own query rather than storing candidates."""

    async def _draft(self, sessions, catalogue):  # type: ignore[no-untyped-def]
        services, silpo, _ = services_for(sessions, mcp=FakeSilpo(catalogue))
        await on_text(services, USER, "купи молоко і хліб")
        basket = await services.baskets.get_active(USER)
        assert basket is not None
        return services, silpo, basket.id

    async def test_the_next_candidate_replaces_the_line(self, sessions) -> None:
        catalogue = {
            "молоко": [product("Молоко Перше", 42.90), product("Молоко Друге", 51.00)],
            "хліб": [product("Хліб", 28.50)],
        }
        services, _, basket_id = await self._draft(sessions, catalogue)
        before = await services.baskets.load_cart(basket_id)
        assert before is not None and before.lines[0].name == "Молоко Перше"

        reply = await on_callback(services, USER, f"swap:{basket_id}:0")
        assert "Молоко Друге" in reply.text
        assert reply.toast is not None and "Молоко Друге" in reply.toast

        after = await services.baskets.load_cart(basket_id)
        assert after is not None
        assert after.lines[0].name == "Молоко Друге"
        assert after.lines[1].name == "Хліб", "other lines are untouched"

    async def test_swapping_recomputes_the_total(self, sessions) -> None:
        catalogue = {
            "молоко": [product("Молоко Перше", 42.90), product("Молоко Друге", 51.00)],
            "хліб": [product("Хліб", 28.50)],
        }
        services, _, basket_id = await self._draft(sessions, catalogue)
        await on_callback(services, USER, f"swap:{basket_id}:0")
        after = await services.baskets.load_cart(basket_id)
        assert after is not None and after.total == Decimal("130.50"), "2 x 51.00 + 28.50"

    async def test_cycling_wraps_back_to_the_first(self, sessions) -> None:
        catalogue = {
            "молоко": [product("Молоко Перше", 42.90), product("Молоко Друге", 51.00)],
            "хліб": [product("Хліб", 28.50)],
        }
        services, _, basket_id = await self._draft(sessions, catalogue)
        await on_callback(services, USER, f"swap:{basket_id}:0")
        await on_callback(services, USER, f"swap:{basket_id}:0")
        after = await services.baskets.load_cart(basket_id)
        assert after is not None and after.lines[0].name == "Молоко Перше"

    async def test_a_line_with_no_alternative_says_so(self, sessions) -> None:
        catalogue = {"молоко": [product("Молоко", 42.90)], "хліб": [product("Хліб", 28.50)]}
        services, _, basket_id = await self._draft(sessions, catalogue)
        reply = await on_callback(services, USER, f"swap:{basket_id}:0")
        assert "Інших варіантів" in reply.text

    async def test_an_out_of_range_position_is_refused(self, sessions) -> None:
        catalogue = {"молоко": [product("Молоко", 42.90)], "хліб": [product("Хліб", 28.50)]}
        services, _, basket_id = await self._draft(sessions, catalogue)
        assert (await on_callback(services, USER, f"swap:{basket_id}:99")).toast is not None

    async def test_someone_else_cannot_swap_your_basket(self, sessions) -> None:
        catalogue = {"молоко": [product("Молоко", 42.90)], "хліб": [product("Хліб", 28.50)]}
        services, _, basket_id = await self._draft(sessions, catalogue)
        reply = await on_callback(services, 9999, f"swap:{basket_id}:0")
        assert reply.toast == "Ця чернетка недоступна"
