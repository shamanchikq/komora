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

import math
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
from komora.core.models import ResolvedCart
from komora.core.passes.budget import OVER_BUDGET, apply_budget
from komora.core.passes.promos import apply_savings
from komora.core.passes.removals import match_removals
from komora.core.passes.resolve import snap_quantity
from komora.core.pipeline import (
    CartContextMissing,
    SilpoCache,
    build_cart,
    categories_for,
    load_context,
)
from komora.core.sync import cart_product_ids, execute_sync, preview_sync
from komora.db.repo import BasketRepo, ConversationRepo, UserRepo
from komora.db.tables import DraftBasketRow

HISTORY_TURNS = 20

MAX_TEXT = 4096
"""Telegram's own message limit, restated because the Mini App is not Telegram.

The bot inherits this ceiling for free — Telegram will not deliver a longer message.
`POST /api/draft` inherits nothing, so an unbounded body went straight into the
conversation table and into the model prompt. It is enforced here rather than on the
pydantic model so both surfaces get the same answer, in Ukrainian, instead of one
getting a 422 whose body is not an Outcome.
"""

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
UNEXPECTED = (
    "Сталася неочікувана помилка. Спробуйте, будь ласка, ще раз — "
    "якщо повториться, напишіть трохи пізніше."
)
STALE = "Ця чернетка вже неактуальна — напишіть, що потрібно, і зберемо нову."
CANCELLED = "Скасовано. Чернетку прибрано, у кошику Сільпо нічого не змінилося."
NOTHING_TO_SEND = "У цій чернетці нема чого надсилати."
NOTHING_OPTIONAL = "У цій чернетці нема необовʼязкових позицій — прибирати нічого."
BUDGET_HELP = (
    "Тижневий бюджет допомагає бачити, коли кошик виходить за межі.\n"
    "«/budget 1500» — встановити, «/budget 0» — прибрати."
)
NO_ALTERNATIVE = "Інших варіантів для «{name}» Сільпо не пропонує."
TOO_LONG = (
    "Це задовге повідомлення. Напишіть коротше — одним-двома реченнями про те, що потрібно купити."
)
NOTHING_LEFT = (
    "У кошику Сільпо вже нема чого міняти — те, що ця чернетка мала прибрати, "
    "звідти вже зникло. Нічого надсилати не будемо."
)


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
    if len(text) > MAX_TEXT:
        # Before the history read and before anything is persisted: a body this size is
        # not a shopping request, and storing it would poison every later turn's prompt.
        return Spoke(TOO_LONG)
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

    if action == "cancel":
        return await on_cancel(services, telegram_id, basket_id_int)
    if action == "sync":
        return await on_preview(services, telegram_id, basket_id_int)
    if action == "push":
        return await on_push(services, telegram_id, basket_id_int)
    if action == "swap":
        return await on_swap(services, telegram_id, basket_id_int, position)
    return Spoke(STALE, toast="Невідома дія")


async def _own_draft(
    services: Services, telegram_id: int, basket_id: int
) -> DraftBasketRow | Spoke:
    """The checks every basket-scoped request owes its sender.

    A basket id arrives from the client on both surfaces — a Telegram callback and an
    HTTP path are equally guessable — so ownership is re-derived here, never trusted.
    The same gate refuses anything that is not an open draft: after a sync or a
    discard, a replayed id must not act again.

    Returns the row, or the `Spoke` refusal to show instead. The Mini App routes call
    this too; there is no second copy of these rules to fall out of sync.
    """
    basket = await services.baskets.get(basket_id)
    if basket is None or basket.user_id != telegram_id:
        # Ids come from the client; a mismatch is either stale UI or somebody guessing.
        return Spoke(STALE, toast="Ця чернетка недоступна")
    if basket.status != "draft":
        return Spoke(STALE, toast="Чернетка вже неактуальна")
    return basket


async def on_cancel(services: Services, telegram_id: int, basket_id: int) -> Outcome:
    gate = await _own_draft(services, telegram_id, basket_id)
    if isinstance(gate, Spoke):
        return gate
    await services.baskets.set_status(basket_id, "discarded")
    return Spoke(CANCELLED)


async def on_preview(services: Services, telegram_id: int, basket_id: int) -> Outcome:
    gate = await _own_draft(services, telegram_id, basket_id)
    if isinstance(gate, Spoke):
        return gate
    return await _preview(services, telegram_id, basket_id)


async def on_push(services: Services, telegram_id: int, basket_id: int) -> Outcome:
    gate = await _own_draft(services, telegram_id, basket_id)
    if isinstance(gate, Spoke):
        return gate
    return await _push(services, telegram_id, basket_id)


async def on_open_basket(services: Services, telegram_id: int, basket_id: int) -> Outcome:
    """Show an existing draft without changing it — what a deep link points *at*.

    Every other basket route acts; this one only looks, so it re-resolves nothing and
    writes nothing back. (`_draft_ready` still re-derives the over-budget warning from
    the stored total and the current cap — that is reading two saved facts, not
    revisiting Silpo.) The gate is the same: a basket id inside a launch payload is
    as guessable as one inside a callback, and `startapp=` is chosen by whoever opens
    the link. Ownership is re-derived here exactly as it is for a tap.
    """
    gate = await _own_draft(services, telegram_id, basket_id)
    if isinstance(gate, Spoke):
        return gate

    cart = await services.baskets.load_cart(basket_id)
    if cart is None:
        return Spoke(STALE)
    return await _draft_ready(services, telegram_id, basket_id, gate.title, cart)


async def on_swap(services: Services, telegram_id: int, basket_id: int, position: int) -> Outcome:
    gate = await _own_draft(services, telegram_id, basket_id)
    if isinstance(gate, Spoke):
        return gate
    return await _swap(services, telegram_id, basket_id, position, gate.title)


async def on_set_qty(
    services: Services, telegram_id: int, basket_id: int, position: int, qty: float
) -> Outcome:
    """The stepper's target. Quantities are rounded where `clamp_quantity` rounds,
    capped at the line's known stock, and refused below any positive amount."""
    gate = await _own_draft(services, telegram_id, basket_id)
    if isinstance(gate, Spoke):
        return gate

    cart = await services.baskets.load_cart(basket_id)
    if cart is None or not 0 <= position < len(cart.lines):
        return Spoke(STALE, toast="Ця позиція недоступна")
    line = cart.lines[position]
    if line.unavailable:
        return Spoke(STALE, toast="Цієї позиції немає в наявності")

    # Not finite is not positive. A stepper cannot send `NaN`, but JSON has the
    # literal and every guard here used to pass it through — `round` keeps it, it is
    # not `<= 0`, and `min(nan, stock)` is `nan` — so the first thing that refused it
    # was the NOT NULL column, as an unhandled 500.
    wanted = round(qty, 3) if math.isfinite(qty) else 0.0
    if wanted <= 0:
        return Spoke(STALE, toast="Кількість має бути більша за нуль")

    # The same grid `resolve` puts a quantity on, not merely the same ceiling. Capping
    # at stock was all this did, so 2,5 упаковки молока persisted — Silpo counts packs
    # — and 0,37 кг of a good sold in 0,25 steps was an amount Silpo does not sell.
    # A stepper tap is a deliberate amount, so `clamp_quantity`'s "unqualified means
    # one step" rule is deliberately NOT applied; see `snap_quantity`.
    wanted = snap_quantity(wanted, step=line.step, weighted=line.weighted, stock=line.stock)
    if wanted <= 0:
        # Only reachable on a line whose stock read back as zero without being marked
        # unavailable. Nothing to set, and a zero quantity is not a removal.
        return Spoke(STALE, toast="Цієї позиції немає в наявності")

    if not await services.baskets.set_qty(basket_id, position, wanted):
        return Spoke(STALE, toast="Ця позиція недоступна")
    return await _edited_outcome(services, telegram_id, basket_id, gate.title)


async def on_remove_line(
    services: Services, telegram_id: int, basket_id: int, position: int
) -> Outcome:
    """✕ on a row. Removal here only edits the *draft* — the Silpo cart is touched,
    as ever, behind the preview-and-push two-step."""
    gate = await _own_draft(services, telegram_id, basket_id)
    if isinstance(gate, Spoke):
        return gate

    if not await services.baskets.drop_item(basket_id, position):
        return Spoke(STALE, toast="Ця позиція недоступна")
    return await _edited_outcome(services, telegram_id, basket_id, gate.title)


async def on_trim_optional(services: Services, telegram_id: int, basket_id: int) -> Outcome:
    """«Прибрати необовʼязкові» — every optional line still sendable, in one
    confirmation-sized action rather than a row of separate taps."""
    gate = await _own_draft(services, telegram_id, basket_id)
    if isinstance(gate, Spoke):
        return gate

    cart = await services.baskets.load_cart(basket_id)
    if cart is None:
        return Spoke(STALE)
    positions = [i for i, line in enumerate(cart.lines) if line.optional and not line.unavailable]
    if not positions:
        # Not «нема чого надсилати»: there may be plenty to send, just nothing marked
        # optional. Saying the wrong one reads as a failure of the basket.
        return Spoke(NOTHING_OPTIONAL)
    for position in reversed(positions):
        await services.baskets.drop_item(basket_id, position)
    return await _edited_outcome(services, telegram_id, basket_id, gate.title)


async def _edited_outcome(
    services: Services,
    telegram_id: int,
    basket_id: int,
    title: str,
    toast: str | None = None,
) -> Outcome:
    """Reload after a line-level edit and rebuild what the edit invalidated.

    A swap, a quantity or a removal all change the same things: the total and the
    per-line savings notes. Only the per-line discounts are regenerated; coupon notes
    belong to the account, not to which cheese is in the basket.
    """
    cart = await services.baskets.load_cart(basket_id)
    if cart is None:
        return Spoke(STALE)

    total = sum((ln.line_total for ln in cart.lines if not ln.unavailable), Decimal("0"))
    cart = cart.model_copy(update={"total": total})
    cart = apply_savings(cart.model_copy(update={"savings_notes": []}))
    await services.baskets.update_totals(basket_id, cart)
    return await _draft_ready(services, telegram_id, basket_id, title, cart, toast)


async def _draft_ready(
    services: Services,
    telegram_id: int,
    basket_id: int,
    title: str,
    cart: ResolvedCart,
    toast: str | None = None,
) -> DraftReady:
    """The one place a persisted basket becomes a `DraftReady`, so the budget cap can
    never be attached on one path and forgotten on another.

    The over-budget warning is re-derived here for the same reason. It is a fact about
    `total` against `cap`, and every edit route changes the total — so the one the
    pipeline stored went stale the moment «прибрати необовʼязкові» did its job, and the
    screen then carried «Понад бюджет на 84,30 ₴» directly above a bar reading
    «лишається 1,50 ₴». Recomputed rather than persisted: it is derived from two
    things already stored, and a derived value written down is one that can disagree.
    """
    user = await services.users.get(telegram_id)
    cap = user.budget_weekly if user else None
    kept = [w for w in cart.warnings if not w.startswith(f"{OVER_BUDGET}:")]
    cart = apply_budget(cart.model_copy(update={"warnings": kept}), cap)
    return DraftReady(
        title=title,
        cart=cart,
        budget_cap=cap,
        basket_id=basket_id,
        toast=toast,
    )


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
    except CartContextMissing:
        return Spoke(NO_CONTEXT)
    except McpError:
        return Spoke(SILPO_DOWN)

    if alternative is None:
        return Spoke(NO_ALTERNATIVE.format(name=line.name), toast="Інших варіантів нема")

    await services.baskets.replace_item(basket_id, position, alternative)
    return await _edited_outcome(
        services,
        telegram_id,
        basket_id,
        title,
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
    except CartContextMissing:
        return Spoke(NO_CONTEXT)
    except McpError:
        return Spoke(SILPO_DOWN)

    # The check above was made against the draft; this one is made against the cart as
    # Silpo holds it right now. A removals-only basket whose target the user has since
    # taken out by hand arrives here with nothing to add and nothing to remove, and a
    # confirmation sheet that asks to «Додати 0 позицій» is not a question anyone can
    # answer. Both surfaces are spared it by refusing here rather than by drawing it.
    if preview.adding_count == 0 and not preview.removing:
        return Spoke(NOTHING_LEFT)

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
    except CartContextMissing:
        return Spoke(NO_CONTEXT)
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
