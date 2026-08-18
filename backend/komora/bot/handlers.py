"""What the bot does, with Telegram kept at arm's length.

Every handler is a plain async function over `(services, telegram_id, …)` returning an
`Outcome` — a decision carrying domain objects, with no idea how it will be shown.
`bot/render.py: to_reply` turns one into a Telegram message and `bot.py` sends it; a
Mini App serialises the same object and draws its own screen.

They used to return a `Reply` of Telegram HTML, which made "the seam the Mini App will
use" untrue in the way that mattered: a second surface needs the cart, not markup
describing it.

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

from komora.bot.outcomes import DraftReady, Outcome, PreviewReady, Spoke, Synced
from komora.core.agent.loop import ForbiddenToolCall, run_agent
from komora.core.agent.recap import draft_recap, sync_recap
from komora.core.agent.tools import ToolSource
from komora.core.alternatives import next_alternative
from komora.core.llm.protocol import LLMClient, LLMUnavailable, Message
from komora.core.mcp.errors import McpError, NotAuthenticated
from komora.core.mcp.protocol import SilpoClient
from komora.core.passes.promos import apply_savings
from komora.core.passes.removals import match_removals
from komora.core.pipeline import (
    CartContextMissing,
    SilpoCache,
    build_cart,
    categories_for,
    load_context,
)
from komora.core.sync import cart_product_ids, execute_sync, preview_sync
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
    verifier: LLMClient | None = None
    """The model for the verification pass. Falls back to `llm` when unset.

    Worth a second client because Gemini's free-tier quota is keyed on
    (project, model) — Google's own 429 payloads name
    `GenerateRequestsPerDayPerProjectPerModel-FreeTier` with the model as a dimension.
    A basket costs two requests, so pointing the two jobs at two models draws them from
    two independent daily allowances instead of halving one. They also want different
    things: the proposal wants a model that reliably names a Silpo category, the
    verification wants one that returns a usable re-search query.
    """
    verify: bool = True
    """Run the verification pass. One extra model request per basket, which is the
    scarce resource on Gemini's free tier — set false if requests per day bite."""
    start_linking: Callable[[int], Awaitable[None]] = _unlinked
    """Kicks off account linking. Returns immediately — the authorization URL arrives
    as its own message, because the OAuth round-trip can take minutes."""


def _needs_link(text: str) -> Spoke:
    return Spoke(text, needs_link=True)


async def on_start(services: Services, telegram_id: int) -> Outcome:
    await services.users.ensure(telegram_id)
    blob, _ = await services.users.get_token_blob(telegram_id)
    return Spoke(READY) if blob else _needs_link(WELCOME)


async def on_budget(services: Services, telegram_id: int, argument: str) -> Outcome:
    await services.users.ensure(telegram_id)
    argument = argument.strip()

    if not argument:
        user = await services.users.get(telegram_id)
        cap = user.budget_weekly if user else None
        current = f"Зараз тижневий бюджет: {cap} ₴." if cap else "Бюджет не встановлено."
        return Spoke(f"{current}\n\n{BUDGET_HELP}")

    try:
        amount = int(argument.replace(" ", "").replace("₴", ""))
    except ValueError:
        return Spoke(BUDGET_HELP)

    if amount <= 0:
        await services.users.set_budget(telegram_id, None)
        return Spoke("Бюджет прибрано.")
    await services.users.set_budget(telegram_id, amount)
    return Spoke(f"Тижневий бюджет: {amount} ₴. Показуватиму, коли кошик виходить за межі.")


async def on_text(services: Services, telegram_id: int, text: str) -> Outcome:
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
                return Spoke(answer)

            cart = await build_cart(
                outcome.basket,
                mcp,
                context,
                budget_cap=budget_cap,
                llm=(services.verifier or services.llm) if services.verify else None,
                cache=services.cache,
            )
            if outcome.basket.removals:
                cart = cart.model_copy(
                    update={
                        "removals": match_removals(
                            outcome.basket.removals,
                            await services.baskets.synced_lines(telegram_id),
                            keep={ln.product_id for ln in cart.lines if not ln.unavailable},
                            present=await cart_product_ids(mcp),
                        )
                    }
                )
    except NotAuthenticated:
        return _needs_link(NEED_AUTH)
    except CartContextMissing:
        return Spoke(NO_CONTEXT)
    except McpError:
        return Spoke(SILPO_DOWN)
    except LLMUnavailable:
        return Spoke(LLM_DOWN)
    except ForbiddenToolCall:
        # The model reached for a write tool. It never got through — say so plainly
        # rather than showing the user an error they cannot act on.
        return Spoke("Не зміг це опрацювати безпечно. Спробуйте сформулювати інакше.")

    basket = outcome.basket
    # The whole basket, not just its title. A follow-up edit is only possible if the
    # model can see what it is editing — see core/agent/recap.py.
    await services.conversations.append(telegram_id, "assistant", draft_recap(basket.title, cart))

    # Nothing found and nothing to remove: still worth showing, because the cart
    # carries the warnings that say why. Not persisted, so there is nothing to act on.
    if not cart.lines and not cart.removals:
        return DraftReady(title=basket.title, cart=cart, budget_cap=budget_cap)

    basket_id = await services.baskets.create_from_cart(
        telegram_id, basket.title, basket.intent, cart
    )
    return DraftReady(title=basket.title, cart=cart, budget_cap=budget_cap, basket_id=basket_id)


async def on_callback(services: Services, telegram_id: int, data: str) -> Outcome:
    action, _, raw_id = data.partition(":")

    if action == "link":
        await services.start_linking(telegram_id)
        return Spoke(LINK_SENT)

    basket_id, _, raw_position = raw_id.partition(":")
    try:
        position = int(raw_position) if raw_position else -1
        basket_id_int = int(basket_id)
    except ValueError:
        return Spoke(STALE, toast="Невідома дія")

    basket = await services.baskets.get(basket_id_int)
    if basket is None or basket.user_id != telegram_id:
        # Ids come from the client; a mismatch is either stale UI or somebody guessing.
        return Spoke(STALE, toast="Ця чернетка недоступна")
    if basket.status != "draft":
        return Spoke(STALE, toast="Чернетка вже неактуальна")

    if action == "cancel":
        await services.baskets.set_status(basket_id_int, "discarded")
        return Spoke(CANCELLED)
    if action == "sync":
        return await _preview(services, telegram_id, basket_id_int)
    if action == "push":
        return await _push(services, telegram_id, basket_id_int)
    if action == "swap":
        return await _swap(services, telegram_id, basket_id_int, position, basket.title)
    return Spoke(STALE, toast="Невідома дія")


async def _swap(
    services: Services, telegram_id: int, basket_id: int, position: int, title: str
) -> Outcome:
    """Offer the next product Silpo returns for the same query."""
    cart = await services.baskets.load_cart(basket_id)
    if cart is None or not 0 <= position < len(cart.lines):
        return Spoke(STALE, toast="Ця позиція недоступна")

    line = cart.lines[position]
    try:
        async with services.connect(telegram_id) as mcp:
            _, context = await load_context(mcp)
            alternative = await next_alternative(
                line, mcp, context, await categories_for(mcp, context, services.cache)
            )
    except NotAuthenticated:
        return _needs_link(NEED_AUTH)
    except McpError:
        return Spoke(SILPO_DOWN)

    if alternative is None:
        return Spoke(NO_ALTERNATIVE.format(name=line.name), toast="Інших варіантів нема")

    await services.baskets.replace_item(basket_id, position, alternative)
    updated = await services.baskets.load_cart(basket_id)
    if updated is None:
        return Spoke(STALE)

    total = sum((ln.line_total for ln in updated.lines if not ln.unavailable), Decimal("0"))
    updated = updated.model_copy(update={"total": total})
    # Only the per-line discounts are regenerated; coupon notes belong to the
    # account, not to which cheese is in the basket.
    updated = apply_savings(updated.model_copy(update={"savings_notes": []}))
    await services.baskets.update_totals(basket_id, updated)

    user = await services.users.get(telegram_id)
    return DraftReady(
        title=title,
        cart=updated,
        budget_cap=user.budget_weekly if user else None,
        basket_id=basket_id,
        toast=f"Замінено на {alternative.name}"[:200],
    )


async def _preview(services: Services, telegram_id: int, basket_id: int) -> Outcome:
    cart = await services.baskets.load_cart(basket_id)
    if cart is None:
        return Spoke(STALE)
    if not [ln for ln in cart.lines if not ln.unavailable] and not cart.removals:
        return Spoke(NOTHING_TO_SEND)

    try:
        async with services.connect(telegram_id) as mcp:
            _, context = await load_context(mcp)
            preview = await preview_sync(cart, mcp, context)
    except NotAuthenticated:
        return _needs_link(NEED_AUTH)
    except McpError:
        return Spoke(SILPO_DOWN)

    return PreviewReady(basket_id=basket_id, preview=preview)


async def _push(services: Services, telegram_id: int, basket_id: int) -> Outcome:
    cart = await services.baskets.load_cart(basket_id)
    if cart is None:
        return Spoke(STALE)

    try:
        async with services.connect(telegram_id) as mcp:
            report = await execute_sync(cart, mcp)
    except NotAuthenticated:
        return _needs_link(NEED_AUTH)
    except McpError:
        return Spoke(SILPO_DOWN)

    # Only a complete sync closes the draft. A partial one stays open so the same
    # basket can be retried — safe, because re-adding sets quantities rather than
    # incrementing them.
    if report.ok:
        await services.baskets.set_status(basket_id, "synced")

    # What is in the real cart now. The next turn's edit is built on this: a model told
    # only «[чернетка] …» cannot know these products left the draft and became goods.
    await services.conversations.append(telegram_id, "assistant", sync_recap(report))

    return Synced(basket_id=basket_id, report=report)
