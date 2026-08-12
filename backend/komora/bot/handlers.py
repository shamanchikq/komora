"""What the bot does, with Telegram kept at arm's length.

Every handler is a plain async function over `(services, telegram_id, …)` returning a
`Reply`. aiogram appears only in `bot.py`, which adapts these to updates and sends the
result. That is what makes the conversation testable without constructing Telegram
objects, and it is the same seam the Mini App will use later.

Two rules are enforced here rather than trusted:

* **Nothing reaches Silpo without a confirmation.** A draft becomes a preview, and only
  a second, explicit tap sends it.
* **A callback's basket is checked against its sender.** The basket id comes from the
  client, so a user could otherwise sync somebody else's cart by guessing a number.
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from komora.bot.render import render_cart, render_sync_preview, render_sync_report
from komora.core.agent.loop import ForbiddenToolCall, run_agent
from komora.core.agent.recap import draft_recap, sync_recap
from komora.core.agent.tools import ToolSource
from komora.core.alternatives import next_alternative
from komora.core.llm.protocol import LLMClient, LLMUnavailable, Message
from komora.core.mcp.errors import McpError, NotAuthenticated
from komora.core.mcp.protocol import SilpoClient
from komora.core.models import ResolvedCart
from komora.core.passes.promos import apply_savings
from komora.core.passes.removals import match_removals
from komora.core.pipeline import (
    CartContextMissing,
    SilpoCache,
    build_cart,
    categories_for,
    load_context,
)
from komora.core.sync import execute_sync, preview_sync
from komora.db.repo import BasketRepo, ConversationRepo, UserRepo

HISTORY_TURNS = 20

WELCOME = (
    "Комора збирає кошик у «Сільпо» зі звичайного повідомлення: «купи молоко, хліб і "
    "щось до чаю».\n\n"
    "Щоб це працювало, потрібен доступ до вашого акаунта Сільпо — наявність, ціни та "
    "ваш кошик. Комора нічого не додає в кошик без вашого підтвердження і нічого "
    "не оформлює: оплата завжди у Сільпо."
)
READY = "Комору підключено. Скажіть, що потрібно купити."
NEED_AUTH = (
    "Потрібно наново підключити акаунт Сільпо — доступ втратив чинність.\n"
    "Комора нічого не змінює у вашому кошику без підтвердження."
)
LINK_SENT = "Готую посилання для входу в Сільпо…"
NO_CONTEXT = (
    "У вашому кошику Сільпо не вибрано магазин або час доставки, а без них Сільпо не "
    "шукає товари. Оберіть їх у застосунку Сільпо — і напишіть мені ще раз."
)
SILPO_DOWN = "Сільпо зараз не відповідає. Спробуйте, будь ласка, за кілька хвилин."
LLM_DOWN = "Не можу зараз подумати над кошиком — модель недоступна. Спробуйте пізніше."
STALE = "Ця чернетка вже неактуальна — напишіть, що потрібно, і зберемо нову."
CANCELLED = "Скасовано. Чернетку прибрано, у кошику Сільпо нічого не змінилося."
NOTHING_TO_SEND = "У цій чернетці нема чого надсилати."
BUDGET_HELP = (
    "Тижневий бюджет допомагає бачити, коли кошик виходить за межі.\n"
    "«/budget 1500» — встановити, «/budget 0» — прибрати."
)
NO_ALTERNATIVE = "Інших варіантів для «{name}» Сільпо не пропонує."


async def _unlinked(telegram_id: int) -> None:
    raise NotImplementedError("Services.start_linking was not provided")


@dataclass(frozen=True)
class Button:
    label: str
    data: str | None = None
    url: str | None = None
    same_row: bool = False
    """Pack with the button before it. The swap controls are one per line, so a row
    each would bury the actual actions under a wall of buttons."""


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: tuple[Button, ...] = field(default_factory=tuple)
    toast: str | None = None
    """Short answer for a callback query — Telegram shows it without a new message."""


class SilpoConnect(Protocol):
    """Opens a Silpo session for one user. Raises `NotAuthenticated` if unlinked."""

    def __call__(self, telegram_id: int) -> AbstractAsyncContextManager[SilpoClient]: ...


@dataclass(frozen=True)
class Services:
    users: UserRepo
    conversations: ConversationRepo
    baskets: BasketRepo
    llm: LLMClient
    tools: ToolSource
    """Declarations can only be read through an authenticated session, so they are
    fetched on the first turn rather than at startup — see `agent.tools.CachedTools`."""
    connect: SilpoConnect
    cache: SilpoCache = field(default_factory=SilpoCache)
    """Holds Silpo's category tree for the process — see `core.pipeline`."""
    verify: bool = True
    """Run the verification pass. One extra model request per basket, which is the
    scarce resource on Gemini's free tier — set false if requests per day bite."""
    start_linking: Callable[[int], Awaitable[None]] = _unlinked
    """Kicks off account linking. Returns immediately — the authorization URL arrives
    as its own message, because the OAuth round-trip can take minutes."""


def _auth_reply(text: str) -> Reply:
    return Reply(text, buttons=(Button("Підключити Сільпо", data="link"),))


async def on_start(services: Services, telegram_id: int) -> Reply:
    await services.users.ensure(telegram_id)
    blob, _ = await services.users.get_token_blob(telegram_id)
    return Reply(READY) if blob else _auth_reply(WELCOME)


async def on_budget(services: Services, telegram_id: int, argument: str) -> Reply:
    await services.users.ensure(telegram_id)
    argument = argument.strip()

    if not argument:
        user = await services.users.get(telegram_id)
        cap = user.budget_weekly if user else None
        current = f"Зараз тижневий бюджет: {cap} ₴." if cap else "Бюджет не встановлено."
        return Reply(f"{current}\n\n{BUDGET_HELP}")

    try:
        amount = int(argument.replace(" ", "").replace("₴", ""))
    except ValueError:
        return Reply(BUDGET_HELP)

    if amount <= 0:
        await services.users.set_budget(telegram_id, None)
        return Reply("Бюджет прибрано.")
    await services.users.set_budget(telegram_id, amount)
    return Reply(f"Тижневий бюджет: {amount} ₴. Показуватиму, коли кошик виходить за межі.")


async def on_text(services: Services, telegram_id: int, text: str) -> Reply:
    """One user turn: history -> agent -> pipeline -> a draft to review."""
    await services.users.ensure(telegram_id)
    history = [
        Message(role="assistant" if row.role == "assistant" else "user", content=row.content)
        for row in await services.conversations.last_n(telegram_id, HISTORY_TURNS)
    ]
    await services.conversations.append(telegram_id, "user", text)

    user = await services.users.get(telegram_id)
    budget_cap = user.budget_weekly if user else None

    try:
        async with services.connect(telegram_id) as mcp:
            _, context = await load_context(mcp)
            outcome = await run_agent(
                llm=services.llm,
                mcp=mcp,
                context=context,
                history=history,
                user_message=text,
                tools=await services.tools(mcp),
            )
            if outcome.basket is None:
                answer = outcome.reply or SILPO_DOWN
                await services.conversations.append(telegram_id, "assistant", answer)
                return Reply(answer)

            cart = await build_cart(
                outcome.basket,
                mcp,
                context,
                budget_cap=budget_cap,
                llm=services.llm if services.verify else None,
                cache=services.cache,
            )
    except NotAuthenticated:
        return _auth_reply(NEED_AUTH)
    except CartContextMissing:
        return Reply(NO_CONTEXT)
    except McpError:
        return Reply(SILPO_DOWN)
    except LLMUnavailable:
        return Reply(LLM_DOWN)
    except ForbiddenToolCall:
        # The model reached for a write tool. It never got through — say so plainly
        # rather than showing the user an error they cannot act on.
        return Reply("Не зміг це опрацювати безпечно. Спробуйте сформулювати інакше.")

    basket = outcome.basket
    if basket.removals:
        cart = cart.model_copy(
            update={
                "removals": match_removals(
                    basket.removals,
                    await services.baskets.synced_lines(telegram_id),
                    keep={ln.product_id for ln in cart.lines if not ln.unavailable},
                )
            }
        )

    rendered = render_cart(cart, basket.title, budget_cap=budget_cap, swappable=True)
    # The whole basket, not just its title. A follow-up edit is only possible if the
    # model can see what it is editing — see core/agent/recap.py.
    await services.conversations.append(telegram_id, "assistant", draft_recap(basket.title, cart))

    if not cart.lines and not cart.removals:
        return Reply(rendered)

    basket_id = await services.baskets.create_from_cart(
        telegram_id, basket.title, basket.intent, cart
    )
    return Reply(rendered, buttons=_draft_buttons(basket_id, cart))


MAX_SWAP_BUTTONS = 8
"""Telegram tolerates more, but a keyboard longer than this reads as noise."""


def _draft_buttons(basket_id: int, cart: ResolvedCart) -> tuple[Button, ...]:
    """Send/cancel, plus one «⇄ N» per line matching the numbers in the text."""
    swaps = tuple(
        Button(f"⇄ {i}", data=f"swap:{basket_id}:{i - 1}", same_row=i > 1)
        for i, line in enumerate(cart.lines[:MAX_SWAP_BUTTONS], start=1)
        if not line.unavailable
    )
    return (
        *swaps,
        Button("Надіслати в Сільпо", data=f"sync:{basket_id}"),
        Button("Скасувати", data=f"cancel:{basket_id}"),
    )


async def on_callback(services: Services, telegram_id: int, data: str) -> Reply:
    action, _, raw_id = data.partition(":")

    if action == "link":
        await services.start_linking(telegram_id)
        return Reply(LINK_SENT)

    basket_id, _, raw_position = raw_id.partition(":")
    try:
        position = int(raw_position) if raw_position else -1
        basket_id_int = int(basket_id)
    except ValueError:
        return Reply(STALE, toast="Невідома дія")

    basket = await services.baskets.get(basket_id_int)
    if basket is None or basket.user_id != telegram_id:
        # Ids come from the client; a mismatch is either stale UI or somebody guessing.
        return Reply(STALE, toast="Ця чернетка недоступна")
    if basket.status != "draft":
        return Reply(STALE, toast="Чернетка вже неактуальна")

    if action == "cancel":
        await services.baskets.set_status(basket_id_int, "discarded")
        return Reply(CANCELLED)
    if action == "sync":
        return await _preview(services, telegram_id, basket_id_int)
    if action == "push":
        return await _push(services, telegram_id, basket_id_int)
    if action == "swap":
        return await _swap(services, telegram_id, basket_id_int, position, basket.title)
    return Reply(STALE, toast="Невідома дія")


async def _swap(
    services: Services, telegram_id: int, basket_id: int, position: int, title: str
) -> Reply:
    """Offer the next product Silpo returns for the same query."""
    cart = await services.baskets.load_cart(basket_id)
    if cart is None or not 0 <= position < len(cart.lines):
        return Reply(STALE, toast="Ця позиція недоступна")

    line = cart.lines[position]
    try:
        async with services.connect(telegram_id) as mcp:
            _, context = await load_context(mcp)
            alternative = await next_alternative(
                line, mcp, context, await categories_for(mcp, context, services.cache)
            )
    except NotAuthenticated:
        return _auth_reply(NEED_AUTH)
    except McpError:
        return Reply(SILPO_DOWN)

    if alternative is None:
        return Reply(NO_ALTERNATIVE.format(name=line.name), toast="Інших варіантів нема")

    await services.baskets.replace_item(basket_id, position, alternative)
    updated = await services.baskets.load_cart(basket_id)
    if updated is None:
        return Reply(STALE)

    total = sum((ln.line_total for ln in updated.lines if not ln.unavailable), Decimal("0"))
    updated = updated.model_copy(update={"total": total})
    # Only the per-line discounts are regenerated; coupon notes belong to the
    # account, not to which cheese is in the basket.
    updated = apply_savings(updated.model_copy(update={"savings_notes": []}))
    await services.baskets.update_totals(basket_id, updated)

    user = await services.users.get(telegram_id)
    return Reply(
        render_cart(
            updated, title, budget_cap=user.budget_weekly if user else None, swappable=True
        ),
        buttons=_draft_buttons(basket_id, updated),
        toast=f"Замінено на {alternative.name}"[:200],
    )


async def _preview(services: Services, telegram_id: int, basket_id: int) -> Reply:
    cart = await services.baskets.load_cart(basket_id)
    if cart is None:
        return Reply(STALE)
    if not [ln for ln in cart.lines if not ln.unavailable] and not cart.removals:
        return Reply(NOTHING_TO_SEND)

    try:
        async with services.connect(telegram_id) as mcp:
            _, context = await load_context(mcp)
            preview = await preview_sync(cart, mcp, context)
    except NotAuthenticated:
        return _auth_reply(NEED_AUTH)
    except McpError:
        return Reply(SILPO_DOWN)

    return Reply(
        render_sync_preview(preview),
        buttons=(
            Button("Додати в кошик", data=f"push:{basket_id}"),
            Button("Скасувати", data=f"cancel:{basket_id}"),
        ),
    )


async def _push(services: Services, telegram_id: int, basket_id: int) -> Reply:
    cart = await services.baskets.load_cart(basket_id)
    if cart is None:
        return Reply(STALE)

    try:
        async with services.connect(telegram_id) as mcp:
            report = await execute_sync(cart, mcp)
    except NotAuthenticated:
        return _auth_reply(NEED_AUTH)
    except McpError:
        return Reply(SILPO_DOWN)

    # Only a complete sync closes the draft. A partial one stays open so the same
    # basket can be retried — safe, because re-adding sets quantities rather than
    # incrementing them.
    if report.ok:
        await services.baskets.set_status(basket_id, "synced")

    # What is in the real cart now. The next turn's edit is built on this: a model told
    # only «[чернетка] …» cannot know these products left the draft and became goods.
    await services.conversations.append(telegram_id, "assistant", sync_recap(report))

    buttons: list[Button] = []
    if report.checkout_web_link:
        buttons.append(Button("Оформити на сайті", url=report.checkout_web_link))
    if not report.ok:
        buttons.append(Button("Спробувати ще раз", data=f"push:{basket_id}"))

    return Reply(render_sync_report(report), buttons=tuple(buttons))
