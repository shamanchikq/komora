"""The conversation, end to end, with Telegram and the network removed.

These drive the handler functions directly against a real (in-memory) database, a
scripted LLM and the Silpo fake, so the whole loop — message, draft, preview, sync — is
exercised without an aiogram object anywhere.

Handlers return an `Outcome`, not a rendered message. Assertions about *wording* go
through `to_reply`, which is what the bot does; assertions about *decisions* read the
outcome directly, which is what the Mini App will do.
"""

import contextlib
from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from komora.bot.handlers import (
    NEED_AUTH,
    NO_ACTIVE_DRAFT,
    NO_CONTEXT,
    SILPO_DOWN,
    Services,
    on_budget,
    on_callback,
    on_open_active,
    on_open_basket,
    on_remove_line,
    on_start,
    on_text,
)
from komora.bot.outcomes import DraftReady, PreviewReady, Spoke, Synced
from komora.bot.render import to_reply
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
        reply = to_reply(await on_start(services, USER))
        assert "доступ до вашого акаунта Сільпо" in reply.text
        assert "нічого не додає" in reply.text, "the promise has to be made up front"
        assert button_data(reply) == ["link"]

    async def test_linked_user_is_ready_to_go(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        await services.users.set_token_blob(USER, b"encrypted", None)
        reply = to_reply(await on_start(services, USER))
        assert "підключено" in reply.text.lower()
        assert reply.buttons == ()

    async def test_the_link_button_starts_the_flow(self, sessions) -> None:
        services, _, linked = services_for(sessions)
        to_reply(await on_callback(services, USER, "link"))
        assert linked == [USER]


class TestConversation:
    async def test_a_plain_question_is_answered_as_text(self, sessions) -> None:
        llm = ScriptedLLM(LLMResponse(text="Є грузинське вино за 420 ₴"))
        services, _, _ = services_for(sessions, llm=llm)
        reply = to_reply(await on_text(services, USER, "яке грузинське вино є до 500 ₴?"))
        assert reply.text == "Є грузинське вино за 420 ₴"
        assert reply.buttons == ()

    async def test_a_stated_list_becomes_a_reviewable_draft(self, sessions) -> None:
        services, silpo, _ = services_for(sessions)
        reply = to_reply(await on_text(services, USER, "купи молоко і хліб"))

        assert "Молоко Яготинське" in reply.text and "Хліб Київський" in reply.text
        assert reply.text.count("— ви попросили") == 2, "every line carries its reason"
        actions = [d and d.split(":")[0] for d in button_data(reply)]
        assert actions == ["swap", "swap", "sync", "cancel"], "one swap per line"
        assert silpo.add_calls == [], "nothing may reach the cart before confirmation"

    async def test_the_draft_is_persisted_for_the_confirm_tap(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        to_reply(await on_text(services, USER, "купи молоко і хліб"))
        basket = await services.baskets.get_active(USER)
        assert basket is not None and basket.status == "draft"
        cart = await services.baskets.load_cart(basket.id)
        assert cart is not None and len(cart.lines) == 2

    async def test_history_is_recorded_without_duplicating_the_new_message(self, sessions) -> None:
        llm = ScriptedLLM(LLMResponse(text="перша"), LLMResponse(text="друга"))
        services, _, _ = services_for(sessions, llm=llm)
        to_reply(await on_text(services, USER, "привіт"))
        to_reply(await on_text(services, USER, "ще раз"))

        second_prompt = [m.content for m in llm.calls[1]["messages"]]
        assert second_prompt == ["привіт", "перша", "ще раз"]

    async def test_a_budget_cap_is_applied_to_the_draft(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        await services.users.ensure(USER)
        await services.users.set_budget(USER, 50)
        reply = to_reply(await on_text(services, USER, "купи молоко і хліб"))
        assert "перевищено" in reply.text

    async def test_nothing_found_produces_no_send_button(self, sessions) -> None:
        services, _, _ = services_for(sessions, mcp=FakeSilpo({}))
        reply = to_reply(await on_text(services, USER, "купи молоко і хліб"))
        assert reply.buttons == ()
        assert "Не знайшлося" in reply.text


class TestFailurePaths:
    async def test_an_unlinked_account_is_offered_the_login(self, sessions) -> None:
        services, _, _ = services_for(sessions, connect_error=NotAuthenticated())
        reply = to_reply(await on_text(services, USER, "купи молоко"))
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
        reply = to_reply(await on_text(services, USER, "купи молоко"))
        assert reply.text == NO_CONTEXT

    async def test_silpo_being_down_is_reported_as_such(self, sessions) -> None:
        services, _, _ = services_for(sessions, connect_error=McpUnavailable("down"))
        assert (to_reply(await on_text(services, USER, "купи молоко"))).text == SILPO_DOWN


class TestConfirmation:
    async def _draft(self, sessions, **kw):  # type: ignore[no-untyped-def]
        services, silpo, _ = services_for(sessions, **kw)
        to_reply(await on_text(services, USER, "купи молоко і хліб"))
        basket = await services.baskets.get_active(USER)
        assert basket is not None
        return services, silpo, basket.id

    async def test_confirming_shows_a_preview_before_anything_is_sent(self, sessions) -> None:
        services, silpo, basket_id = await self._draft(sessions)
        reply = to_reply(await on_callback(services, USER, f"sync:{basket_id}"))
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
        reply = to_reply(await on_callback(services, USER, f"sync:{basket_id}"))
        assert "вже 1 позиція" in reply.text and "не чіпаємо" in reply.text

    async def test_pushing_adds_to_the_cart_and_offers_checkout(self, sessions) -> None:
        services, silpo, basket_id = await self._draft(sessions)
        to_reply(await on_callback(services, USER, f"sync:{basket_id}"))
        reply = to_reply(await on_callback(services, USER, f"push:{basket_id}"))

        assert "Готово" in reply.text
        assert [b.url for b in reply.buttons] == ["https://silpo.ua/checkout/abc"]
        sent = [p["productId"] for batch in silpo.add_calls for p in batch]
        assert sorted(sent) == ["id-Молоко Яготинське 2,6%", "id-Хліб Київський"]

    async def test_a_completed_sync_closes_the_draft(self, sessions) -> None:
        services, _, basket_id = await self._draft(sessions)
        to_reply(await on_callback(services, USER, f"push:{basket_id}"))
        assert await services.baskets.get_status(basket_id) == "synced"

    async def test_a_partial_failure_keeps_the_draft_open_for_a_retry(self, sessions) -> None:
        silpo = FakeSilpo(CATALOGUE, reject={"id-Хліб Київський"})
        services, _, basket_id = await self._draft(sessions, mcp=silpo)
        reply = to_reply(await on_callback(services, USER, f"push:{basket_id}"))

        assert "Вийшло не все" in reply.text
        assert "Хліб Київський" in reply.text
        assert await services.baskets.get_status(basket_id) == "draft"
        assert "push" in str(button_data(reply)), "a retry has to be offered"

    async def test_cancelling_discards_the_draft_and_touches_nothing(self, sessions) -> None:
        services, silpo, basket_id = await self._draft(sessions)
        reply = to_reply(await on_callback(services, USER, f"cancel:{basket_id}"))
        assert "у кошику Сільпо нічого не змінилося" in reply.text
        assert await services.baskets.get_status(basket_id) == "discarded"
        assert silpo.add_calls == []

    async def test_a_stale_draft_cannot_be_synced_twice(self, sessions) -> None:
        services, silpo, basket_id = await self._draft(sessions)
        to_reply(await on_callback(services, USER, f"push:{basket_id}"))
        before = len(silpo.add_calls)
        reply = to_reply(await on_callback(services, USER, f"push:{basket_id}"))
        assert reply.toast is not None
        assert len(silpo.add_calls) == before, "a second tap must not re-send"


class TestOwnership:
    async def test_a_basket_belonging_to_someone_else_is_refused(self, sessions) -> None:
        """The id arrives from the client, so ownership is checked, not assumed."""
        services, silpo, _ = services_for(sessions)
        to_reply(await on_text(services, USER, "купи молоко і хліб"))
        basket = await services.baskets.get_active(USER)
        assert basket is not None

        intruder = 9999
        reply = to_reply(await on_callback(services, intruder, f"push:{basket.id}"))
        assert reply.toast == "Ця чернетка недоступна"
        assert silpo.add_calls == []
        assert await services.baskets.get_status(basket.id) == "draft"

    async def test_an_unknown_basket_is_refused(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        assert (to_reply(await on_callback(services, USER, "push:12345"))).toast is not None

    @pytest.mark.parametrize("data", ["push:абв", "нісенітниця", "sync:"])
    async def test_malformed_callback_data_is_survivable(self, sessions, data: str) -> None:
        services, _, _ = services_for(sessions)
        assert (to_reply(await on_callback(services, USER, data))).text


class TestBudgetCommand:
    async def test_setting_and_clearing(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        assert "1500" in (to_reply(await on_budget(services, USER, "1500"))).text
        user = await services.users.get(USER)
        assert user is not None and user.budget_weekly == 1500

        to_reply(await on_budget(services, USER, "0"))
        user = await services.users.get(USER)
        assert user is not None and user.budget_weekly is None

    async def test_showing_the_current_value(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        to_reply(await on_budget(services, USER, "800"))
        assert "800 ₴" in (to_reply(await on_budget(services, USER, ""))).text

    async def test_nonsense_gets_the_help_text(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        assert "/budget" in (to_reply(await on_budget(services, USER, "багато"))).text


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

    to_reply(await on_start(services, USER))
    draft = to_reply(await on_text(services, USER, "купи молоко і хліб"))
    basket_id = int(str(draft.buttons[0].data).split(":")[1])

    preview = to_reply(await on_callback(services, USER, f"sync:{basket_id}"))
    assert "не чіпаємо" in preview.text

    report = to_reply(await on_callback(services, USER, f"push:{basket_id}"))
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
        to_reply(await on_text(services, USER, "купи молоко і хліб"))
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

        reply = to_reply(await on_callback(services, USER, f"swap:{basket_id}:0"))
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
        to_reply(await on_callback(services, USER, f"swap:{basket_id}:0"))
        after = await services.baskets.load_cart(basket_id)
        assert after is not None and after.total == Decimal("130.50"), "2 x 51.00 + 28.50"

    async def test_cycling_wraps_back_to_the_first(self, sessions) -> None:
        catalogue = {
            "молоко": [product("Молоко Перше", 42.90), product("Молоко Друге", 51.00)],
            "хліб": [product("Хліб", 28.50)],
        }
        services, _, basket_id = await self._draft(sessions, catalogue)
        to_reply(await on_callback(services, USER, f"swap:{basket_id}:0"))
        to_reply(await on_callback(services, USER, f"swap:{basket_id}:0"))
        after = await services.baskets.load_cart(basket_id)
        assert after is not None and after.lines[0].name == "Молоко Перше"

    async def test_a_line_with_no_alternative_says_so(self, sessions) -> None:
        catalogue = {"молоко": [product("Молоко", 42.90)], "хліб": [product("Хліб", 28.50)]}
        services, _, basket_id = await self._draft(sessions, catalogue)
        reply = to_reply(await on_callback(services, USER, f"swap:{basket_id}:0"))
        assert "Інших варіантів" in reply.text

    async def test_an_out_of_range_position_is_refused(self, sessions) -> None:
        catalogue = {"молоко": [product("Молоко", 42.90)], "хліб": [product("Хліб", 28.50)]}
        services, _, basket_id = await self._draft(sessions, catalogue)
        assert (
            to_reply(await on_callback(services, USER, f"swap:{basket_id}:99"))
        ).toast is not None

    async def test_someone_else_cannot_swap_your_basket(self, sessions) -> None:
        catalogue = {"молоко": [product("Молоко", 42.90)], "хліб": [product("Хліб", 28.50)]}
        services, _, basket_id = await self._draft(sessions, catalogue)
        reply = to_reply(await on_callback(services, 9999, f"swap:{basket_id}:0"))
        assert reply.toast == "Ця чернетка недоступна"


SWAPPABLE = {
    "молоко": [product("Молоко Перше", 42.90), product("Молоко Друге", 51.00)],
    "хліб": [product("Хліб", 28.50)],
}


class TestSwapPreservesTheRest:
    """A swap changes one product. Everything else about the basket must survive."""

    async def test_coupon_notes_survive(self, sessions) -> None:
        """They belong to the account, not to which milk is in the basket. Recomputing
        the discounts used to wipe them, so «-10% на онлайн чек» vanished on a tap."""
        coupon = {"id": 1, "active": True, "description": "-10% на онлайн чек"}
        services, _, _ = services_for(sessions, mcp=FakeSilpo(SWAPPABLE, coupons=[coupon]))
        to_reply(await on_text(services, USER, "купи молоко і хліб"))
        basket = await services.baskets.get_active(USER)
        assert basket is not None

        before = await services.baskets.load_cart(basket.id)
        assert before is not None and before.coupon_notes == ["-10% на онлайн чек"]

        reply = to_reply(await on_callback(services, USER, f"swap:{basket.id}:0"))
        after = await services.baskets.load_cart(basket.id)
        assert after is not None
        assert after.coupon_notes == ["-10% на онлайн чек"]
        assert "-10% на онлайн чек" in reply.text

    async def test_discount_notes_are_regenerated_not_appended(self, sessions) -> None:
        catalogue = {
            "молоко": [
                product("Молоко Перше", 42.90, old_price=50.00),
                product("Молоко Друге", 51.00, old_price=60.00),
            ],
            "хліб": [product("Хліб", 28.50)],
        }
        services, _, _ = services_for(sessions, mcp=FakeSilpo(catalogue))
        to_reply(await on_text(services, USER, "купи молоко і хліб"))
        basket = await services.baskets.get_active(USER)
        assert basket is not None

        to_reply(await on_callback(services, USER, f"swap:{basket.id}:0"))
        after = await services.baskets.load_cart(basket.id)
        assert after is not None
        assert len(after.savings_notes) == 1, "one line, one note — not two"
        assert "Молоко Друге" in after.savings_notes[0]


REPLACEMENT_CALL = ToolCall(
    PROPOSE_BASKET,
    {
        "title": "Заміна ковбаски",
        "lines": [{"description": "салямі", "quantity": 1, "reason_text": "ви попросили"}],
        "removals": ["хліб"],
    },
)


class TestEditingWhatIsAlreadyInTheCart:
    """The reported bug, in full.

    «Додай інгредієнти для піци» → sent to Silpo → «заміни ковбаски на салямі» added a
    second sausage and left the first one sitting in the cart. An edit could only ever
    append, because nothing in the bot could take a product back out.
    """

    async def _synced(self, sessions, silpo: FakeSilpo) -> Services:
        """Get to the state the bug needs: a basket already in the real Silpo cart."""
        services, _, _ = services_for(sessions, mcp=silpo)
        to_reply(await on_text(services, USER, "молоко і хліб"))
        basket_id = (await services.baskets.get_active(USER)).id
        to_reply(await on_callback(services, USER, f"sync:{basket_id}"))
        to_reply(await on_callback(services, USER, f"push:{basket_id}"))
        assert {p["name"] for p in silpo._cart} == {"Молоко Яготинське 2,6%", "Хліб Київський"}
        return services

    async def test_a_replacement_removes_the_product_it_replaces(self, sessions) -> None:
        silpo = FakeSilpo(CATALOGUE | {"салямі": [product("Ковбаса Салямі", 123.00)]})
        services = await self._synced(sessions, silpo)

        services.llm._responses.append(LLMResponse(tool_calls=(REPLACEMENT_CALL,)))
        reply = to_reply(await on_text(services, USER, "заміни хліб на салямі"))
        assert "Приберемо з кошика" in reply.text and "Хліб Київський" in reply.text

        basket_id = (await services.baskets.get_active(USER)).id
        preview = to_reply(await on_callback(services, USER, f"sync:{basket_id}"))
        assert "Приберемо з кошика" in preview.text, "the tap that authorises it says so"

        to_reply(await on_callback(services, USER, f"push:{basket_id}"))
        names = {p["name"] for p in silpo._cart}
        assert names == {"Молоко Яготинське 2,6%", "Ковбаса Салямі"}, (
            "the replaced product must be gone, and the untouched one must remain"
        )

    async def test_only_what_komora_synced_is_a_candidate(self, sessions) -> None:
        """A product the user put in their own cart is never removed, however well the
        words match — Komora cannot tell it from one of its own."""
        silpo = FakeSilpo(
            CATALOGUE | {"салямі": [product("Ковбаса Салямі", 123.00)]},
            existing=[
                {
                    "productId": "id-Хліб Домашній",
                    "companyId": "c",
                    "branchId": "b",
                    "name": "Хліб Домашній",
                    "price": 30.0,
                    "quantity": 1,
                }
            ],
        )
        services, _, _ = services_for(sessions, mcp=silpo)
        services.llm._responses.append(LLMResponse(tool_calls=(REPLACEMENT_CALL,)))

        reply = to_reply(await on_text(services, USER, "заміни хліб на салямі"))
        assert "Приберемо з кошика" not in reply.text

        basket_id = (await services.baskets.get_active(USER)).id
        to_reply(await on_callback(services, USER, f"sync:{basket_id}"))
        to_reply(await on_callback(services, USER, f"push:{basket_id}"))
        assert "id-Хліб Домашній" in {p["productId"] for p in silpo._cart}

    async def test_the_removal_survives_the_two_taps(self, sessions) -> None:
        """The draft is persisted between the tap that shows it and the tap that sends
        it, so the removal has to be stored, not recomputed."""
        silpo = FakeSilpo(CATALOGUE | {"салямі": [product("Ковбаса Салямі", 123.00)]})
        services = await self._synced(sessions, silpo)
        services.llm._responses.append(LLMResponse(tool_calls=(REPLACEMENT_CALL,)))
        to_reply(await on_text(services, USER, "заміни хліб на салямі"))

        reloaded = await services.baskets.load_cart((await services.baskets.get_active(USER)).id)
        assert [r.name for r in reloaded.removals] == ["Хліб Київський"]


class TestGroundingTheNextTurn:
    async def test_the_model_is_told_what_the_basket_held(self, sessions) -> None:
        """History used to carry the title alone, so a follow-up edit had nothing to
        edit and the model invented a replacement basket."""
        services, _, _ = services_for(sessions)
        to_reply(await on_text(services, USER, "молоко і хліб"))

        recorded = [m.content for m in await services.conversations.last_n(USER)]
        assert any("Молоко Яготинське 2,6%" in text for text in recorded)
        assert any("Хліб Київський" in text for text in recorded)

    async def test_the_model_is_told_the_products_reached_the_real_cart(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        to_reply(await on_text(services, USER, "молоко і хліб"))
        basket_id = (await services.baskets.get_active(USER)).id
        to_reply(await on_callback(services, USER, f"sync:{basket_id}"))
        to_reply(await on_callback(services, USER, f"push:{basket_id}"))

        recorded = [m.content for m in await services.conversations.last_n(USER)]
        assert any("надіслано в кошик Сільпо" in text for text in recorded)


class TestTwoModelRouting:
    """A basket costs two model requests, and free-tier quota is per (project, model).

    Pointing the proposal and the verification at different models draws them from two
    independent daily allowances rather than halving one.
    """

    async def test_the_verifier_gets_the_verification_pass(self, sessions) -> None:
        agent_llm = ScriptedLLM(LLMResponse(tool_calls=(BASKET_CALL,)))
        verifier = ScriptedLLM(LLMResponse(text="ок"))
        services, _, _ = services_for(sessions, llm=agent_llm, verify=True)
        services = replace(services, verifier=verifier)

        to_reply(await on_text(services, USER, "купи молоко і хліб"))
        assert len(agent_llm.calls) == 1, "the agent loop used the agent's model"
        assert len(verifier.calls) == 1, "the verification pass used the verifier"

    async def test_it_falls_back_to_the_one_model_when_unset(self, sessions) -> None:
        """Every existing caller passes a single client and must keep working."""
        llm = ScriptedLLM(LLMResponse(tool_calls=(BASKET_CALL,)), LLMResponse(text="ок"))
        services, _, _ = services_for(sessions, llm=llm, verify=True)
        assert services.verifier is None

        to_reply(await on_text(services, USER, "купи молоко і хліб"))
        assert len(llm.calls) == 2, "both jobs fell back to the same client"

    async def test_no_verifier_request_when_verification_is_off(self, sessions) -> None:
        verifier = ScriptedLLM(LLMResponse(text="ок"))
        services, _, _ = services_for(sessions, verify=False)
        services = replace(services, verifier=verifier)

        to_reply(await on_text(services, USER, "купи молоко і хліб"))
        assert verifier.calls == []


class TestAStaleRemovalIsNotPromised:
    """Komora's record says what it PUT in the cart, not what is still there.

    Live on 2026-08-12: a cheese removed one turn earlier was still offered for removal
    on the next draft — «Приберемо з кошика: …» — and then nothing happened, because
    only the confirmation sheet ever checked the request against the real cart.
    """

    REMOVE_CALL = ToolCall(
        PROPOSE_BASKET,
        {
            "title": "Паста",
            "lines": [{"description": "молоко", "quantity": 1, "reason_text": "ви попросили"}],
            "removals": ["хліб"],
        },
    )

    async def _synced_then_emptied(self, sessions, silpo: FakeSilpo) -> Services:
        services, _, _ = services_for(sessions, mcp=silpo)
        to_reply(await on_text(services, USER, "молоко і хліб"))
        basket_id = (await services.baskets.get_active(USER)).id
        to_reply(await on_callback(services, USER, f"sync:{basket_id}"))
        to_reply(await on_callback(services, USER, f"push:{basket_id}"))
        # The user takes it out themselves, in the Silpo app.
        silpo._cart = [p for p in silpo._cart if p["productId"] != "id-Хліб Київський"]
        return services

    async def test_a_product_already_gone_is_not_offered_for_removal(self, sessions) -> None:
        silpo = FakeSilpo(CATALOGUE)
        services = await self._synced_then_emptied(sessions, silpo)

        services.llm._responses.append(LLMResponse(tool_calls=(self.REMOVE_CALL,)))
        reply = to_reply(await on_text(services, USER, "прибери хліб"))
        assert "Приберемо з кошика" not in reply.text, (
            "the draft must not promise a removal the sync will silently skip"
        )

    async def test_a_product_still_there_is_offered(self, sessions) -> None:
        """The guard must not swallow the real case."""
        silpo = FakeSilpo(CATALOGUE)
        services, _, _ = services_for(sessions, mcp=silpo)
        to_reply(await on_text(services, USER, "молоко і хліб"))
        basket_id = (await services.baskets.get_active(USER)).id
        to_reply(await on_callback(services, USER, f"sync:{basket_id}"))
        to_reply(await on_callback(services, USER, f"push:{basket_id}"))

        services.llm._responses.append(LLMResponse(tool_calls=(self.REMOVE_CALL,)))
        reply = to_reply(await on_text(services, USER, "прибери хліб"))
        assert "Приберемо з кошика" in reply.text and "Хліб Київський" in reply.text


class TestTheSeamASecondSurfaceWillUse:
    """A handler hands back a decision, not a rendering of one.

    These read the outcome directly — no `to_reply` — which is what the Mini App does.
    If any of this needs HTML parsed out of a string, the seam has regressed.
    """

    async def test_a_draft_arrives_as_a_cart_not_as_markup(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        outcome = await on_text(services, USER, "молоко і хліб")

        assert isinstance(outcome, DraftReady)
        assert outcome.basket_id is not None, "persisted, so a surface can act on it"
        assert [line.name for line in outcome.cart.lines] == [
            "Молоко Яготинське 2,6%",
            "Хліб Київський",
        ]
        assert outcome.cart.total > 0
        assert all(line.reason_text for line in outcome.cart.lines), "every line says why"

    async def test_an_empty_draft_carries_its_reason_and_nothing_to_act_on(self, sessions) -> None:
        services, _, _ = services_for(sessions, mcp=FakeSilpo({}))
        outcome = await on_text(services, USER, "молоко і хліб")

        assert isinstance(outcome, DraftReady)
        assert outcome.cart.lines == []
        assert outcome.basket_id is None, "nothing persisted, so nothing to act on"
        assert any(w.startswith("not_found:") for w in outcome.cart.warnings)

    async def test_the_confirmation_sheet_is_a_preview_object(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        draft = await on_text(services, USER, "молоко і хліб")
        assert isinstance(draft, DraftReady) and draft.basket_id is not None

        outcome = await on_callback(services, USER, f"sync:{draft.basket_id}")

        assert isinstance(outcome, PreviewReady)
        assert outcome.preview.adding_count == 2
        assert outcome.preview.adding_total > 0

    async def test_a_sync_reports_what_landed(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        draft = await on_text(services, USER, "молоко і хліб")
        assert isinstance(draft, DraftReady) and draft.basket_id is not None
        await on_callback(services, USER, f"sync:{draft.basket_id}")

        outcome = await on_callback(services, USER, f"push:{draft.basket_id}")

        assert isinstance(outcome, Synced)
        assert outcome.report.ok is True
        assert sorted(outcome.report.added) == ["Молоко Яготинське 2,6%", "Хліб Київський"]

    async def test_prose_outcomes_say_whether_linking_is_offered(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        outcome = await on_start(services, USER)
        assert isinstance(outcome, Spoke)
        assert outcome.needs_link is True, "an unlinked user is offered the login"


class TestAPartialSyncIsVisibleAfterwards:
    """A push that lands partly leaves the basket open **on purpose** — it has to be
    retriable, and re-adding sets quantities rather than incrementing them.

    But an open basket is drawn as an ordinary draft on both surfaces, under the
    promise that Silpo is untouched, and for the lines that already landed that is
    false. Nothing recorded which those were: «synced» lived on the basket, and a
    partly-landed basket is not one.
    """

    async def _partial(self, sessions: async_sessionmaker):  # type: ignore[no-untyped-def]
        """Milk lands, bread is swallowed — accepted, then silently not in the cart."""
        mcp = FakeSilpo(CATALOGUE, swallow={"id-Хліб Київський"})
        services, _, _ = services_for(sessions, mcp=mcp)
        draft = await on_text(services, USER, "купи молоко і хліб")
        assert isinstance(draft, DraftReady) and draft.basket_id is not None
        synced = await on_callback(services, USER, f"push:{draft.basket_id}")
        assert isinstance(synced, Synced)
        return services, draft.basket_id, synced

    async def test_the_basket_stays_open_for_a_retry(self, sessions: async_sessionmaker) -> None:
        services, basket_id, synced = await self._partial(sessions)
        assert synced.report.ok is False
        assert synced.report.added == ["Молоко Яготинське 2,6%"]
        assert await services.baskets.get_status(basket_id) == "draft"

    async def test_the_line_that_landed_is_marked(self, sessions: async_sessionmaker) -> None:
        services, basket_id, _ = await self._partial(sessions)
        reopened = await on_open_basket(services, USER, basket_id)
        assert isinstance(reopened, DraftReady)
        marked = {line.name: line.synced for line in reopened.cart.lines}
        assert marked == {"Молоко Яготинське 2,6%": True, "Хліб Київський": False}

    async def test_the_draft_says_so_rather_than_promising_otherwise(
        self, sessions: async_sessionmaker
    ) -> None:
        services, basket_id, _ = await self._partial(sessions)
        reopened = await on_open_basket(services, USER, basket_id)
        text = to_reply(reopened).text
        assert "вже в кошику Сільпо" in text

    async def test_a_landed_line_can_be_removed_by_name(self, sessions: async_sessionmaker) -> None:
        """`synced_lines` read the basket's status, so a product Komora had genuinely
        put in the cart minutes earlier was not a removal candidate at all."""
        services, _, _ = await self._partial(sessions)
        candidates = await services.baskets.synced_lines(USER)
        assert [line.name for line in candidates] == ["Молоко Яготинське 2,6%"]

    async def test_a_clean_sync_marks_every_line(self, sessions: async_sessionmaker) -> None:
        """The guard must not have narrowed the ordinary case."""
        services, _, _ = services_for(sessions)
        draft = await on_text(services, USER, "купи молоко і хліб")
        assert isinstance(draft, DraftReady) and draft.basket_id is not None
        await on_callback(services, USER, f"push:{draft.basket_id}")
        candidates = await services.baskets.synced_lines(USER)
        assert {line.name for line in candidates} == {
            "Молоко Яготинське 2,6%",
            "Хліб Київський",
        }


class TestTheActiveDraftIsReachable:
    """«/basket» — a draft card scrolls away, and building another discards it."""

    async def test_it_returns_the_open_draft(self, sessions: async_sessionmaker) -> None:
        services, _, _ = services_for(sessions)
        draft = await on_text(services, USER, "купи молоко і хліб")
        assert isinstance(draft, DraftReady)
        again = await on_open_active(services, USER)
        assert isinstance(again, DraftReady)
        assert again.basket_id == draft.basket_id
        assert [ln.name for ln in again.cart.lines] == [ln.name for ln in draft.cart.lines]

    async def test_no_draft_is_said_plainly(self, sessions: async_sessionmaker) -> None:
        services, _, _ = services_for(sessions)
        assert await on_open_active(services, USER) == Spoke(NO_ACTIVE_DRAFT)

    async def test_it_is_scoped_to_the_sender(self, sessions: async_sessionmaker) -> None:
        services, _, _ = services_for(sessions)
        await on_text(services, USER, "купи молоко і хліб")
        assert await on_open_active(services, 999) == Spoke(NO_ACTIVE_DRAFT)

    async def test_a_sent_basket_is_no_longer_active(self, sessions: async_sessionmaker) -> None:
        services, _, _ = services_for(sessions)
        draft = await on_text(services, USER, "купи молоко і хліб")
        assert isinstance(draft, DraftReady)
        await on_callback(services, USER, f"push:{draft.basket_id}")
        assert await on_open_active(services, USER) == Spoke(NO_ACTIVE_DRAFT)


class TestStrikingARowOffTheDraftLeavesTheCartAlone:
    """✕ edits the draft and nothing else — so a product Komora already put in the
    Silpo cart is still there afterwards, and still Komora's to take back out.

    `synced_lines` filtered `removed` rows out, which made exactly that product the
    one thing «прибери молоко» could not name: the draft had forgotten it, the cart
    had not, and the only surface able to remove it said it had nothing to remove.
    """

    REMOVE_MILK = ToolCall(
        PROPOSE_BASKET,
        {"title": "Прибрати молоко", "lines": [], "removals": ["молоко"]},
    )

    async def _landed_then_struck(self, sessions):  # type: ignore[no-untyped-def]
        """Milk lands in the cart, bread is swallowed, then ✕ on the milk row."""
        mcp = FakeSilpo(CATALOGUE, swallow={"id-Хліб Київський"})
        services, _, _ = services_for(sessions, mcp=mcp)
        draft = await on_text(services, USER, "купи молоко і хліб")
        assert isinstance(draft, DraftReady) and draft.basket_id is not None
        await on_callback(services, USER, f"push:{draft.basket_id}")
        assert "id-Молоко Яготинське 2,6%" in {p["productId"] for p in mcp._cart}

        struck = await on_remove_line(services, USER, draft.basket_id, 0)
        assert isinstance(struck, DraftReady)
        assert [ln.name for ln in struck.cart.lines] == ["Хліб Київський"]
        return services, mcp

    async def test_the_silpo_cart_is_untouched_by_the_strike(self, sessions) -> None:
        _, mcp = await self._landed_then_struck(sessions)
        assert "id-Молоко Яготинське 2,6%" in {p["productId"] for p in mcp._cart}

    async def test_the_struck_product_is_still_a_removal_candidate(self, sessions) -> None:
        services, _ = await self._landed_then_struck(sessions)
        assert [ln.name for ln in await services.baskets.synced_lines(USER)] == [
            "Молоко Яготинське 2,6%"
        ]

    async def test_the_chat_can_still_take_it_out(self, sessions) -> None:
        services, mcp = await self._landed_then_struck(sessions)
        services.llm._responses.append(LLMResponse(tool_calls=(self.REMOVE_MILK,)))

        outcome = await on_text(services, USER, "прибери молоко")
        assert isinstance(outcome, DraftReady)
        assert [r.name for r in outcome.cart.removals] == ["Молоко Яготинське 2,6%"]

        assert outcome.basket_id is not None
        await on_callback(services, USER, f"push:{outcome.basket_id}")
        assert "id-Молоко Яготинське 2,6%" not in {p["productId"] for p in mcp._cart}


class TestBudgetBounds:
    async def test_a_number_no_column_can_hold_gets_the_help_text(self, sessions) -> None:
        """`Integer` is 32 bits on Postgres and 64 on SQLite; past either the driver
        raised where the help text belonged."""
        services, _, _ = services_for(sessions)
        reply = to_reply(await on_budget(services, USER, "99999999999999999999"))
        assert "/budget 1500" in reply.text
        user = await services.users.get(USER)
        assert user is not None and user.budget_weekly is None, "nothing was stored"

    async def test_a_large_but_sane_budget_is_accepted(self, sessions) -> None:
        services, _, _ = services_for(sessions)
        to_reply(await on_budget(services, USER, "1000000"))
        user = await services.users.get(USER)
        assert user is not None and user.budget_weekly == 1_000_000
