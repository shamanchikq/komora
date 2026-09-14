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

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from komora.bot.outcomes import (
    AlternativesReady,
    Ask,
    DealReady,
    DealsReady,
    DraftReady,
    HabitsReady,
    NudgeReady,
    Outcome,
    PreviewReady,
    Spoke,
    Synced,
    TrackedDeal,
)
from komora.core.agent.loop import ForbiddenToolCall, run_agent
from komora.core.agent.recap import cancel_recap, draft_recap, sync_recap
from komora.core.agent.tools import ToolSource
from komora.core.alternatives import list_alternatives, next_alternative
from komora.core.deals.models import Snapshot
from komora.core.deals.scan import (
    USUAL_WINDOW_DAYS,
    deals_among,
    percent_below,
    promo_texts,
    rank_branch_deals,
    snapshot_tracked,
    usual_price,
)
from komora.core.digest import DigestInput, ExpiringCoupon, digest_text
from komora.core.habits.draft import HABITS_INTENT, HABITS_TITLE, habit_lines
from komora.core.habits.engine import Habit, compute_habits, due, due_to_buy
from komora.core.habits.importer import ImportReport, import_history
from komora.core.habits.purchases import KYIV
from komora.core.llm.protocol import LLMClient, LLMUnavailable, Message
from komora.core.mcp.errors import McpError, NotAuthenticated
from komora.core.mcp.gateway import Busy
from komora.core.mcp.protocol import SilpoClient
from komora.core.models import KnownLine, ResolvedCart, SearchContext
from komora.core.money import uah
from komora.core.passes.budget import OVER_BUDGET, apply_budget
from komora.core.passes.promos import apply_savings, coupon_usable, describe_coupons
from komora.core.passes.removals import match_removals
from komora.core.passes.resolve import snap_quantity
from komora.core.pipeline import (
    CartContextMissing,
    SilpoCache,
    TimeslotExpired,
    _coupons,
    _listed,
    build_cart,
    build_known_cart,
    categories_for,
    ensure_timeslot,
    load_context,
)
from komora.core.sync import cart_product_ids, execute_sync, preview_sync
from komora.core.text import days, pl
from komora.db.repo import (
    BasketRepo,
    ConversationRepo,
    HabitRepo,
    HistoryImportRepo,
    NotificationRepo,
    PriceSnapshotRepo,
    PurchaseRepo,
    ReceiptRepo,
    UserRepo,
)
from komora.db.tables import DraftBasketRow, User

log = logging.getLogger(__name__)

HISTORY_TURNS = 20

MAX_ROW_ID = 2**63 - 1
"""The largest integer a row id can hold, on either store this project targets.

Not a guess at how many baskets there will be: it is the point past which the
*database driver* fails rather than answering. See `_own_draft`.
"""

MAX_BUDGET = 1_000_000
"""Sanity ceiling for «/budget N», in ₴.

`budget_weekly` is an `Integer` column: SQLite takes 64 bits and Postgres 32, so a
number past either reached the driver — an `OverflowError` here, a `DataError` there —
as an unhandled error where the help text belonged. No household budgets a million a
week; the narrow miss is nobody and the wide miss was a crash on a typo.
"""

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
SLOT_EXPIRED = (
    "Час доставки у вашому кошику Сільпо вже минув, а за минулим часом Сільпо не "
    "знаходить жодного товару. Оберіть новий час у застосунку Сільпо — і спробуйте ще раз."
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
NO_ACTIVE_DRAFT = "Зараз нема відкритої чернетки. Напишіть, що потрібно купити, — і зберемо нову."
NOTHING_OPTIONAL = "У цій чернетці нема необовʼязкових позицій — прибирати нічого."
BUDGET_HELP = (
    "Тижневий бюджет допомагає бачити, коли кошик виходить за межі.\n"
    "«/budget 1500» — встановити, «/budget 0» — прибрати."
)
QUIET_HELP = (
    "Тихі години — коли Комора нічого не надсилає сама, за київським часом.\n"
    "«/quiet 22 8» — з 22:00 до 08:00, «/quiet off» — надсилати будь-коли."
)
NO_ALTERNATIVE = "Інших варіантів для «{name}» Сільпо не пропонує."
TOO_LONG = (
    "Це задовге повідомлення. Напишіть коротше — одним-двома реченнями про те, що потрібно купити."
)
NOTHING_LEFT = (
    "У кошику Сільпо вже нема чого міняти — те, що ця чернетка мала прибрати, "
    "звідти вже зникло. Нічого надсилати не будемо."
)
HABITS_OFF = "Звички ще не увімкнено на цьому сервері."
NO_HABITS_TO_BUILD = (
    "Нема з чого зібрати: серед ваших покупок ще не видно повторюваних, або все, що "
    "видно, ви попросили не відстежувати."
)
NOTHING_MUTED = "Ви нічого не вимикали — Комора відстежує все, що бачить у чеках."
UNKNOWN_HABIT = "Такої позиції серед ваших звичок нема."
MUTED_TOAST = "Більше не відстежую"
UNMUTED_TOAST = "Знову відстежую"
DISMISSED = "Добре. Нагадаю, коли знову буде пора."
DELETE_ASK = (
    "Видалити все, що Комора знає про вас: доступ до Сільпо, історію покупок, звички "
    "та чернетки? Кошик у Сільпо це не зачепить. Скасувати це буде неможливо."
)
DELETE_YES = "Так, видалити все"
DELETE_KEPT = "Добре, нічого не видаляю."
DELETED = "Готово. Комора більше нічого про вас не зберігає. /start — якщо захочете повернутись."
NOTHING_TO_DELETE = "Комора й так нічого про вас не зберігає."
PAYOFF_INTRO = "Ваші покупки вже в Коморі. Відстежую {items}:"
PAYOFF_OUTRO = "«/usual» — подивитися, «/mute» — вимкнути щось; коли буде пора, нагадаю."
DIGEST_HELP = (
    "Підсумок тижня — одне повідомлення в неділю ввечері: скільки витрачено за чеками, "
    "скільки заощаджено, що, схоже, знадобиться наступного тижня.\n"
    "«/digest on» — увімкнути, «/digest off» — вимкнути."
)
DIGEST_ON = "Підсумок тижня увімкнено — прийде в неділю ввечері. «/digest off» — вимкнути."
DIGEST_OFF = "Підсумок тижня вимкнено."
DEALS_TITLE = "Акції"
DEAL_REASON = "зі знижкою у Сільпо — {price} замість {old}"
DEAL_ADDED_REASON = "додано з акцій"
NOT_A_DEAL = "Цього товару вже нема в акціях або в наявності — нічого не додано."
UNKNOWN_COMMAND = (
    "Такої команди в Комори нема. Ось що вона вміє:\n"
    "/start — підключити Сільпо · /basket — відкрита чернетка · /usual — звичні покупки\n"
    "/deals — акції · /budget — тижневий бюджет · /digest — підсумок тижня\n"
    "/mute — що не відстежувати · /quiet — тихі години · /delete — видалити все\n\n"
    "А щоб зібрати кошик, просто напишіть, що потрібно купити."
)
RESTRICTIONS_ADVISORY = "restrictions:advisory"
"""Silpo reported food restrictions and the model was told them for the *menu*; no line
of the cart was checked against them (Plan 4 D6). Rendered as that sentence."""

NUDGE_KIND = "habit_due"
NUDGE_COOLDOWN = timedelta(days=3)
"""No second nudge about the same product inside this window — a nudge ignored is an
answer, and asking again sooner would be nagging."""
MAX_NUDGE_ITEMS = 5
MAX_PRODUCT_KEY = 64
"""`purchases.product_key` is a `String(64)`; a longer key from a client cannot match
anything and is refused before the query, like an oversized basket id."""


async def _unlinked(telegram_id: int) -> None:
    raise NotImplementedError("Services.start_linking was not provided")


async def _no_notify(telegram_id: int, outcome: Outcome) -> None:
    raise NotImplementedError("Services.notify was not provided")


class Notify(Protocol):
    def __call__(self, telegram_id: int, outcome: Outcome) -> Awaitable[None]: ...


@dataclass(frozen=True)
class HabitServices:
    """The Plan 3 stores, bundled so `Services` grows one optional field, not four."""

    purchases: PurchaseRepo
    habits: HabitRepo
    imports: HistoryImportRepo
    notifications: NotificationRepo
    connect_background: BackgroundConnect | None = None
    """A session that yields to a user's turn instead of queueing behind it —
    `SilpoGateway.connect_background`. Falls back to `Services.connect` when unset."""
    receipts_in_flight: set[int] = field(default_factory=set)
    """Users with an after-turn receipt import already scheduled, so a burst of taps
    schedules one import rather than one per tap. Per process, like the gateway's locks."""
    prices: PriceSnapshotRepo | None = None
    """Plan 4 price memory. `None` switches the deal scan and the alert off."""
    receipts: ReceiptRepo | None = None
    """Plan 4 receipt totals, for the digest. `None` stores lines only."""
    prices_in_flight: set[int] = field(default_factory=set)


class BackgroundConnect(Protocol):
    """`SilpoGateway.connect_background`: a session that gives up with `Busy` after
    `wait_seconds` rather than queueing behind a user's turn indefinitely."""

    def __call__(
        self, telegram_id: int, *, wait_seconds: float = ...
    ) -> AbstractAsyncContextManager[SilpoClient]: ...


class Spawn(Protocol):
    """Run a coroutine nobody awaits — work that must not cost the reply latency."""

    def __call__(self, work: Coroutine[Any, Any, None]) -> None: ...


_detached: set[asyncio.Task[None]] = set()


def run_detached(work: Coroutine[Any, Any, None]) -> None:
    """The production `Spawn`. Keeps a reference until the task ends — the event loop
    holds only a weak one, and a collected task simply never finishes."""
    task = asyncio.get_running_loop().create_task(work)
    _detached.add(task)
    task.add_done_callback(_detached.discard)


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
    habits: HabitServices | None = None
    """Plan 3. `None` keeps every habits route answering `HABITS_OFF` rather than
    crashing — the callback-only tests build a `Services` without it."""
    notify: Notify = _no_notify
    """Say an outcome to a user nobody is replying to — the learning payoff after a
    link, a nudge. Built in `main.py` over the same `render.to_reply` a reply uses."""
    spawn: Spawn = run_detached
    """Where work that must not delay a reply goes — the receipts import a turn
    schedules (`_load_context`). Tests pass one that holds the work until drained."""


def _no_context(exc: CartContextMissing) -> str:
    """No slot at all and a slot that has passed are fixed in the same place, but they
    are not the same sentence."""
    return SLOT_EXPIRED if isinstance(exc, TimeslotExpired) else NO_CONTEXT


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
    if amount > MAX_BUDGET:
        return Spoke(BUDGET_HELP)

    if amount <= 0:
        await services.users.set_budget(telegram_id, None)
        return Spoke("Бюджет прибрано.")
    await services.users.set_budget(telegram_id, amount)
    return Spoke(f"Тижневий бюджет: {amount} ₴. Показуватиму, коли кошик виходить за межі.")


QUIET_HOURS = re.compile(r"\s*(\d{1,2})(?::00)?\s*(?:[-–—]|\s)\s*(\d{1,2})(?::00)?\s*")
QUIET_OFF = frozenset({"off", "вимкнути", "ні"})


def quiet_text(user: User | None) -> str:
    start, end = DEFAULT_QUIET_HOURS
    if user is not None and user.quiet_from is not None and user.quiet_to is not None:
        start, end = user.quiet_from, user.quiet_to
        if start == end:
            return "Тихих годин немає: нагадування можуть прийти будь-коли."
        return f"Тихі години: з {start:02d}:00 до {end:02d}:00."
    return f"Тихі години: з {start:02d}:00 до {end:02d}:00 (типово)."


async def on_quiet(services: Services, telegram_id: int, argument: str) -> Outcome:
    """«/quiet 22 8» — the hours in which nothing is sent unasked.

    `users.quiet_from`/`quiet_to` existed from the habits migration, and `in_quiet_hours`
    read them, but nothing could write them: the default 22–08 Kyiv was the only
    choice. «off» is stored as an empty window (`start == end`), which
    `in_quiet_hours` already reads as "never quiet"; two equal hours typed by hand are
    refused instead, because «з 9 до 9» is more likely a typo than a wish.
    """
    await services.users.ensure(telegram_id)
    argument = argument.strip()
    if not argument:
        return Spoke(f"{quiet_text(await services.users.get(telegram_id))}\n\n{QUIET_HELP}")
    if argument.lower() in QUIET_OFF:
        await services.users.set_quiet_hours(telegram_id, 0, 0)
        return Spoke(quiet_text(await services.users.get(telegram_id)))
    match = QUIET_HOURS.fullmatch(argument)
    if match is None:
        return Spoke(QUIET_HELP)
    start, end = int(match.group(1)), int(match.group(2))
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return Spoke(QUIET_HELP)
    await services.users.set_quiet_hours(telegram_id, start, end)
    return Spoke(
        f"{quiet_text(await services.users.get(telegram_id))} "
        "У цей час Комора нічого не надсилає сама."
    )


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
            _, context = await _load_context(services, telegram_id, mcp)
            household = await household_context(mcp, text)
            outcome = await run_agent(
                llm=services.llm,
                mcp=mcp,
                context=context,
                history=history,
                user_message=f"{text}\n\n{household.lines}" if household.lines else text,
                tools=await services.tools(mcp),
            )
            if outcome.basket is None:
                answer = outcome.reply or SILPO_DOWN
                await services.conversations.append(telegram_id, "assistant", answer)
                return Spoke(answer)

            if household.restricted:
                outcome.basket.warnings.append(RESTRICTIONS_ADVISORY)
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
    except CartContextMissing as exc:
        return Spoke(_no_context(exc))
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


PLAN_HINT = re.compile(
    r"план|меню|тижд|гост|людей|осіб|вечір|свят|шашлик|день народж|дит|діт|рецепт", re.I
)
"""When a turn is worth two account reads before the model sees it (Plan 4 D6, J3):
restrictions and children's ages are read only for a message that looks like a plan
or an event. Both reads need no cart context and answer in a tenth of a second; both
payloads are used for this one request and never stored."""


@dataclass(frozen=True)
class HouseholdContext:
    lines: str = ""
    """What is appended to the user message: «ОБМЕЖЕННЯ: …» and «ДІТИ: …» lines."""
    restricted: bool = False


def restriction_names(payload: Any) -> list[str]:
    """`name` when Silpo gives one, the `slug` otherwise — never invented. The one
    populated value ever seen was `{slug: "all-food", name: null}` (reference §10.5),
    which reads as «all-food» here: unknown, and shown as such."""
    names: list[str] = []
    for entry in _listed(payload, "restrictions", "items", "data"):
        if isinstance(entry, dict):
            label = str(entry.get("name") or entry.get("slug") or "").strip()
            if label:
                names.append(label)
        elif isinstance(entry, str) and entry.strip():
            names.append(entry.strip())
    return names


def children_ages(payload: Any, today: date) -> list[int]:
    """Whole years from `children[].dateOfBirth`; a child with no date is not counted."""
    ages: list[int] = []
    if not isinstance(payload, dict):
        return ages
    for child in payload.get("children") or []:
        if not isinstance(child, dict):
            continue
        raw = str(child.get("dateOfBirth") or "")[:10]
        try:
            born = date.fromisoformat(raw)
        except ValueError:
            continue
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        if 0 <= age < 25:
            ages.append(age)
    return sorted(ages)


async def household_context(mcp: SilpoClient, text: str) -> HouseholdContext:
    """Restrictions and children's ages for a plan or an event, as text for the model.

    A soft constraint on the *proposal*, and said so: the model is asked to avoid the
    restricted products in the menu, and the draft carries `RESTRICTIONS_ADVISORY`
    because no line of the cart is checked — Plan 1 removed the substring filter that
    failed in the dangerous direction, and nothing better exists until a populated
    restrictions payload has been captured. A read that fails adds nothing.
    """
    if not PLAN_HINT.search(text):
        return HouseholdContext()
    lines: list[str] = []
    restricted = False
    try:
        names = restriction_names(await mcp.get_my_food_restrictions())
    except Exception:
        names = []
    if names:
        restricted = True
        lines.append("ОБМЕЖЕННЯ: " + ", ".join(names))
    try:
        ages = children_ages(await mcp.get_my_family(), today_in_kyiv())
    except Exception:
        ages = []
    if ages:
        lines.append("ДІТИ: " + ", ".join(f"{a} р." for a in ages))
    return HouseholdContext(lines="\n".join(lines), restricted=restricted)


async def on_unknown_command(services: Services, telegram_id: int) -> Outcome:
    """A slash command Komora does not have: the list, not a model request.

    `/help`, `/cancel`, a typo — anything that was not a known command fell through to
    the text handler and became a basket request, spending the day's scarcest resource
    on answering confusion (a known issue since Plan 1)."""
    return Spoke(UNKNOWN_COMMAND)


async def on_callback(services: Services, telegram_id: int, data: str) -> Outcome:
    action, _, raw_id = data.partition(":")

    if action == "link":
        await services.start_linking(telegram_id)
        return Spoke(LINK_SENT)
    if action == "dismiss":
        return Spoke(DISMISSED, toast="Добре")
    if action in ("mute", "unmute"):
        return await on_set_mute(services, telegram_id, raw_id, muted=action == "mute")
    if action == "habits" and raw_id in ("build", "nudge"):
        return await on_habits_draft(services, telegram_id, from_nudge=raw_id == "nudge")
    if action == "habits" and raw_id.startswith("deal:"):
        # «Зібрати кошик» under a deal alert: the product the alert named, and only it.
        key = raw_id.partition(":")[2]
        return await on_habits_draft(services, telegram_id, keys=[key] if key else [])
    if action == "habits" and raw_id == "deals":
        # From «/deals»: every tracked product currently discounted.
        return await on_habits_draft(services, telegram_id, discounted_only=True)
    if action == "delete" and raw_id == "confirm":
        return await on_delete_confirm(services, telegram_id)
    if action == "delete" and raw_id == "keep":
        return Spoke(DELETE_KEPT, toast="Нічого не видалено")

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
    if not 0 < basket_id <= MAX_ROW_ID:
        # Before the query, because the query is what breaks. An id no row can hold
        # reaches the driver rather than the gate: SQLite raises OverflowError and
        # Postgres a bigint DataError, so `GET /api/baskets/<26 nines>` was an
        # unhandled 500 instead of this refusal. FastAPI's `int` path type has no
        # ceiling, and neither did `on_callback`'s `int(...)`. The same shape as
        # `POST …/lines/-1/remove`: a client-supplied id that kills the query before
        # ownership is ever asked about.
        return Spoke(STALE, toast="Ця чернетка недоступна")

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
    # What the model reads next turn: without it the history ends on this draft, and the
    # next message is taken as an edit of a basket the user just threw away.
    await services.conversations.append(telegram_id, "assistant", cancel_recap(gate.title))
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


async def on_open_active(services: Services, telegram_id: int) -> Outcome:
    """The draft this user has open right now, if any — «де мій кошик?».

    A draft was reachable only from the message that announced it. The Mini App's menu
    button carries no launch payload, so it always opened on compose; typing there
    calls `create_from_cart`, which discards the previous draft to keep "confirm"
    unambiguous — so the way back to a basket was to *destroy* it. In the chat the
    card scrolls away and there was no command to ask for it either.

    No id crosses the wire, so there is nothing to guess: the draft is looked up *by*
    the sender rather than checked against them. `get_active` already filtered on
    `user_id` and `status == "draft"` and had no production caller at all.
    """
    await services.users.ensure(telegram_id)
    basket = await services.baskets.get_active(telegram_id)
    if basket is None:
        return Spoke(NO_ACTIVE_DRAFT)

    cart = await services.baskets.load_cart(basket.id)
    if cart is None:
        return Spoke(NO_ACTIVE_DRAFT)
    return await _draft_ready(services, telegram_id, basket.id, basket.title, cart)


async def on_swap(services: Services, telegram_id: int, basket_id: int, position: int) -> Outcome:
    gate = await _own_draft(services, telegram_id, basket_id)
    if isinstance(gate, Spoke):
        return gate
    return await _swap(services, telegram_id, basket_id, position, gate.title)


async def on_list_alternatives(
    services: Services, telegram_id: int, basket_id: int, position: int
) -> AlternativesReady | Spoke:
    """What else Silpo has for this line — the picker behind «⇄» in the Mini App.

    Reads only. Nothing is chosen here and nothing is written, so an accidental tap
    costs a search and no more.
    """
    gate = await _own_draft(services, telegram_id, basket_id)
    if isinstance(gate, Spoke):
        return gate

    cart = await services.baskets.load_cart(basket_id)
    if cart is None or not 0 <= position < len(cart.lines):
        return Spoke(STALE, toast="Ця позиція недоступна")

    line = cart.lines[position]
    try:
        async with services.connect(telegram_id) as mcp:
            _, context = await _load_context(services, telegram_id, mcp)
            options = await list_alternatives(
                line, mcp, context, await categories_for(mcp, context, services.cache)
            )
    except NotAuthenticated:
        return _needs_link(NEED_AUTH)
    except CartContextMissing as exc:
        return Spoke(_no_context(exc))
    except McpError:
        return Spoke(SILPO_DOWN)

    return AlternativesReady(basket_id=basket_id, position=position, current=line, options=options)


async def on_choose_alternative(
    services: Services, telegram_id: int, basket_id: int, position: int, product_id: str
) -> Outcome:
    """Put the product the user picked on this line.

    The id comes from the client, so it buys nothing on its own: the candidate list is
    rebuilt and the choice has to be *in* it. That is the same rule as everywhere else
    here — an id is a request, never a permission — and it means what can be chosen is
    exactly what was offered, priced as Silpo prices it now rather than as the picker
    happened to draw it.
    """
    gate = await _own_draft(services, telegram_id, basket_id)
    if isinstance(gate, Spoke):
        return gate

    cart = await services.baskets.load_cart(basket_id)
    if cart is None or not 0 <= position < len(cart.lines):
        return Spoke(STALE, toast="Ця позиція недоступна")

    line = cart.lines[position]
    if product_id == line.product_id:
        # Already chosen. Not an error, and not worth a write.
        return await _draft_ready(services, telegram_id, basket_id, gate.title, cart)

    try:
        async with services.connect(telegram_id) as mcp:
            _, context = await _load_context(services, telegram_id, mcp)
            options = await list_alternatives(
                line, mcp, context, await categories_for(mcp, context, services.cache)
            )
    except NotAuthenticated:
        return _needs_link(NEED_AUTH)
    except CartContextMissing as exc:
        return Spoke(_no_context(exc))
    except McpError:
        return Spoke(SILPO_DOWN)

    chosen = next((o for o in options if o.product_id == product_id), None)
    if chosen is None:
        # The list moved under the user — stock ran out, or the payload was invented.
        return Spoke(STALE, toast="Цього варіанта вже нема")

    await services.baskets.replace_item(basket_id, position, chosen)
    return await _edited_outcome(
        services,
        telegram_id,
        basket_id,
        gate.title,
        toast=f"Обрано {chosen.name}"[:200],
    )


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
            _, context = await _load_context(services, telegram_id, mcp)
            alternative = await next_alternative(
                line, mcp, context, await categories_for(mcp, context, services.cache)
            )
    except NotAuthenticated:
        return _needs_link(NEED_AUTH)
    except CartContextMissing as exc:
        return Spoke(_no_context(exc))
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
            _, context = await _load_context(services, telegram_id, mcp)
            preview = await preview_sync(cart, mcp, context)
    except NotAuthenticated:
        return _needs_link(NEED_AUTH)
    except CartContextMissing as exc:
        return Spoke(_no_context(exc))
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
    except CartContextMissing as exc:
        return Spoke(_no_context(exc))
    except McpError:
        return Spoke(SILPO_DOWN)

    # What actually landed, per line, before anything else. A partial sync leaves the
    # basket open (below), and an open basket is drawn by every draft surface with
    # «у кошику Сільпо нічого не зміниться» under it — false for exactly these lines.
    # Recorded from the report rather than from the attempt: `execute_sync` judges a
    # write by reading the cart back, and this is that reading.
    await services.baskets.mark_synced(basket_id, set(report.added_ids))
    await services.baskets.unmark_synced(telegram_id, set(report.removed_ids))

    # Only a complete sync closes the draft. A partial one stays open so the same
    # basket can be retried — safe, because re-adding sets quantities rather than
    # incrementing them.
    if report.ok:
        await services.baskets.set_status(basket_id, "synced")

    # What is in the real cart now. The next turn's edit is built on this: a model told
    # only «[чернетка] …» cannot know these products left the draft and became goods.
    await services.conversations.append(telegram_id, "assistant", sync_recap(report))

    return Synced(basket_id=basket_id, report=report)


# --- Habits (Plan 3) ------------------------------------------------------------


def today_in_kyiv(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(KYIV).date()


def _habits_or_off(services: Services) -> HabitServices | Spoke:
    return services.habits if services.habits is not None else Spoke(HABITS_OFF)


async def _fresh_at(stores: HabitServices, telegram_id: int) -> datetime | None:
    stamps = [
        await stores.imports.last_ok(telegram_id, "online"),
        await stores.imports.last_ok(telegram_id, "offline"),
    ]
    known = [t for t in stamps if t is not None]
    return max(known) if known else None


RECEIPTS_EVERY = timedelta(days=1)
"""How stale receipts may be before a turn that holds a cart context reads them again."""
AFTER_TURN_WAIT = 120.0
"""How long an after-turn import waits for the turn that scheduled it to let go of the
session. That turn is still running when the import is scheduled — a model request
and a pipeline can take a minute — so the background job's two seconds would read
every single one of these as `Busy`."""
PAYOFF_KIND = "payoff"
PAYOFF_SUBJECT = "habits"


async def _load_context(
    services: Services, telegram_id: int, mcp: SilpoClient
) -> tuple[str, SearchContext]:
    """`pipeline.load_context`, plus the one thing only a turn can do for habits.

    Receipts need the cart's branch and an unexpired timeslot, which a job at an
    arbitrary hour usually does not find. A turn that has just read them does — so
    every turn that reads the cart schedules a receipts import for *after* its reply
    (`refresh_receipts`) when receipts are more than a day old. Before this, receipts
    were read on link and by the job alone, and a household whose slot had lapsed stopped
    being read at all: the last purchase stopped moving and nudges fired about milk
    already bought in the shop.
    """
    cart_id, context = await load_context(mcp, check_slot=False)
    # Receipts first: they read fine against a passed slot, so a turn that is about to
    # be refused for one still keeps the history current.
    await _schedule_receipts(services, telegram_id, context)
    await ensure_timeslot(mcp, context)
    # Prices after the slot check: a search against a passed slot finds nothing, and
    # nothing must not be written down as «unknown» for every tracked product.
    await _schedule_prices(services, telegram_id, context)
    return cart_id, context


PRICES_EVERY = timedelta(days=1)
PRICES_SOURCE = "prices"
"""The `history_imports` source a price scan records under — the same table, so a
skip is a row and «/deals» can say when the tracked products were last re-priced."""


async def _schedule_prices(services: Services, telegram_id: int, context: SearchContext) -> None:
    """The after-turn price scan (Plan 4 Task 2), beside the receipts import.

    A catalogue read needs a live cart context, which a job at an arbitrary hour
    rarely holds — so, as with receipts, the turn that has one schedules the scan for
    after its reply, at most once a day, one in flight per user."""
    stores = services.habits
    if stores is None or stores.prices is None or telegram_id in stores.prices_in_flight:
        return
    last = await stores.imports.last_ok(telegram_id, PRICES_SOURCE)
    if last is not None and datetime.now(UTC) - last < PRICES_EVERY:
        return
    stores.prices_in_flight.add(telegram_id)
    services.spawn(refresh_prices(services, telegram_id, context))


async def refresh_prices(services: Services, telegram_id: int, context: SearchContext) -> None:
    """Re-price the tracked products with the context the turn already read, then say
    something only if one of them is on promotion (`deal_for`). Never raises."""
    stores = services.habits
    if stores is None or stores.prices is None:
        return
    try:
        connect = stores.connect_background
        session = (
            connect(telegram_id, wait_seconds=AFTER_TURN_WAIT)
            if connect is not None
            else services.connect(telegram_id)
        )
        async with session as mcp:
            snapshots = await scan_prices(services, telegram_id, mcp, context)
        alert = await deal_for(services, telegram_id, snapshots)
        if alert is not None:
            await services.notify(telegram_id, alert)
    except Busy:
        await stores.imports.record(
            telegram_id, PRICES_SOURCE, "skipped", "a turn held the session"
        )
    except (NotAuthenticated, McpError) as exc:
        await stores.imports.record(
            telegram_id, PRICES_SOURCE, "failed", f"{type(exc).__name__}: {exc}"
        )
    except Exception:
        log.exception("after-turn price scan failed for %s", telegram_id)
    finally:
        stores.prices_in_flight.discard(telegram_id)


async def scan_prices(
    services: Services,
    telegram_id: int,
    mcp: SilpoClient,
    context: SearchContext,
    *,
    now: datetime | None = None,
) -> list[Snapshot]:
    """One search over the tracked articles; a snapshot per product found, a row in
    `history_imports` either way. A product the search did not return is unknown —
    it gets no snapshot and is named in the row's detail."""
    stores = services.habits
    if stores is None or stores.prices is None:
        return []
    now = now or datetime.now(UTC)
    habits = [h for h in await stores.habits.list(telegram_id) if h.reorderable]
    if not habits:
        await stores.imports.record(telegram_id, PRICES_SOURCE, "ok", "nothing tracked", at=now)
        return []
    try:
        result = await snapshot_tracked(
            mcp, context, habits, today=now.astimezone(KYIV).date(), now=now
        )
    except Exception as exc:
        await stores.imports.record(
            telegram_id, PRICES_SOURCE, "failed", f"{type(exc).__name__}: {exc}", at=now
        )
        raise
    await stores.prices.upsert(telegram_id, result.snapshots)
    detail = f"{len(result.snapshots)} priced" + (
        f", unknown: {', '.join(result.unknown)}" if result.unknown else ""
    )
    await stores.imports.record(telegram_id, PRICES_SOURCE, "ok", detail, at=now)
    return result.snapshots


DEAL_KIND = "deal"
DEAL_COOLDOWN = timedelta(days=7)
"""One deal message per product per week (Plan 4 D9). A promotion that runs for a
month is one piece of news, not four."""
MAX_DEAL_ITEMS = 5


async def _tracked_deals(
    services: Services,
    telegram_id: int,
    snapshots: Sequence[Snapshot] | None = None,
    *,
    branch_id: str | None = None,
) -> list[TrackedDeal]:
    """The tracked, unmuted, reorderable habits whose latest snapshot is a promotion,
    with the «звичайна ціна» comparison when enough history exists."""
    stores = services.habits
    if stores is None or stores.prices is None:
        return []
    habits = {h.product_key: h for h in await stores.habits.list(telegram_id)}
    if snapshots is None:
        if branch_id is None:
            return []
        snapshots = await stores.prices.latest(telegram_id, branch_id)
    out: list[TrackedDeal] = []
    for snapshot in deals_among(snapshots):
        habit = habits.get(snapshot.product_key)
        if habit is None or habit.muted or not habit.reorderable:
            continue
        history = await stores.prices.history(
            telegram_id,
            snapshot.product_key,
            snapshot.branch_id,
            since=snapshot.day - timedelta(days=USUAL_WINDOW_DAYS),
        )
        usual = usual_price(history)
        below = percent_below(snapshot.price, usual) if usual is not None else None
        out.append(TrackedDeal(habit=habit, snapshot=snapshot, below_usual=below))
    return out


async def deal_for(
    services: Services,
    telegram_id: int,
    snapshots: Sequence[Snapshot],
    *,
    now: datetime | None = None,
) -> DealReady | None:
    """The J6 message, or nothing: a *nudgeable* habit on promotion, not just pushed
    to the cart, not told about inside the cooldown, not in quiet hours."""
    stores = services.habits
    if stores is None or stores.prices is None or not snapshots:
        return None
    now = now or datetime.now(UTC)
    if in_quiet_hours(await services.users.get(telegram_id), now):
        return None
    in_cart = await services.baskets.synced_at(telegram_id)
    fresh: list[TrackedDeal] = []
    for deal in await _tracked_deals(services, telegram_id, snapshots):
        if not deal.habit.nudgeable:
            continue
        if already_in_cart(deal.habit, in_cart.get(deal.habit.product_key), now):
            continue
        last = await stores.notifications.last_sent(telegram_id, DEAL_KIND, deal.habit.product_key)
        if last is not None and now - last < DEAL_COOLDOWN:
            continue
        fresh.append(deal)
    if not fresh:
        return None
    chosen = fresh[:MAX_DEAL_ITEMS]
    await stores.notifications.record(
        telegram_id, DEAL_KIND, [d.habit.product_key for d in chosen], at=now
    )
    return DealReady(deals=chosen, today=now.astimezone(KYIV).date())


BRANCH_DEALS_PAGE = 100
DEGRADED_BRANCH = "degraded:branch"
DEGRADED_PROMOS = "degraded:promos"


async def on_deals(services: Services, telegram_id: int) -> Outcome:
    """«/deals» and the «Акції» screen (Plan 4 Task 3).

    Three lists, each computed by Python and each degrading on its own: the tracked
    products on promotion (re-priced now if the last scan is older than a day — the
    user asked), the branch's deepest discounts ranked here, and the account's coupons
    and personal offers as text. No model request anywhere in it.
    """
    await services.users.ensure(telegram_id)
    blob, _ = await services.users.get_token_blob(telegram_id)
    if not blob:
        return _needs_link(WELCOME)
    stores = services.habits
    warnings: list[str] = []
    try:
        async with services.connect(telegram_id) as mcp:
            _, context = await _load_context(services, telegram_id, mcp)
            mine: list[TrackedDeal] = []
            scanned_at: datetime | None = None
            if stores is not None and stores.prices is not None:
                last = await stores.imports.last_ok(telegram_id, PRICES_SOURCE)
                if last is None or datetime.now(UTC) - last >= PRICES_EVERY:
                    snapshots = await scan_prices(services, telegram_id, mcp, context)
                    mine = await _tracked_deals(services, telegram_id, snapshots)
                    scanned_at = datetime.now(UTC)
                else:
                    mine = await _tracked_deals(services, telegram_id, branch_id=context.branch_id)
                    scanned_at = last
            try:
                page = await mcp.get_products(
                    context, mustHavePromotion=True, inStock=True, limit=BRANCH_DEALS_PAGE
                )
                branch = rank_branch_deals(
                    [p for p in (page.get("products") or []) if isinstance(p, dict)]
                )
            except Exception:
                branch, warnings = [], [*warnings, DEGRADED_BRANCH]
            try:
                coupons = describe_coupons(await _coupons(mcp))
            except Exception:
                coupons, warnings = [], [*warnings, "degraded:coupons"]
            try:
                promos = promo_texts(await mcp.get_my_promos())
            except Exception:
                promos, warnings = [], [*warnings, DEGRADED_PROMOS]
    except NotAuthenticated:
        return _needs_link(NEED_AUTH)
    except CartContextMissing as exc:
        return Spoke(_no_context(exc))
    except McpError:
        return Spoke(SILPO_DOWN)
    return DealsReady(
        mine=mine,
        branch=branch,
        coupons=coupons,
        promos=promos,
        scanned_at=scanned_at,
        warnings=warnings,
    )


MAX_DEAL_NAME = 200


async def on_add_deal(
    services: Services,
    telegram_id: int,
    product_id: str,
    name: str,
    external_product_id: int | None,
) -> Outcome:
    """«Додати» on a branch deal: one known-product line into the open draft, or a new
    draft titled «Акції» when there is none.

    The id, the name and the article all arrive from the client, so they buy nothing:
    the line goes through `resolve_known`, which searches by article and pins the hit
    to the id — a product that is not that product, or is not in stock, comes back as
    «не знайшлося» rather than as a line. The price and the reason are what Silpo
    quotes now, not what the screen happened to draw.
    """
    await services.users.ensure(telegram_id)
    product_id, name = product_id.strip(), " ".join(name.split())[:MAX_DEAL_NAME]
    if not product_id or len(product_id) > 64 or not name:
        return Spoke(NOT_A_DEAL, toast="Невідомий товар")
    line = KnownLine(
        product_id=product_id,
        name=name,
        external_product_id=external_product_id,
        reason_kind="deal",
        reason_text=DEAL_ADDED_REASON,
    )
    user = await services.users.get(telegram_id)
    budget_cap = user.budget_weekly if user else None
    try:
        async with services.connect(telegram_id) as mcp:
            _, context = await _load_context(services, telegram_id, mcp)
            cart, _ = await build_known_cart([line], mcp, context, budget_cap=budget_cap)
    except NotAuthenticated:
        return _needs_link(NEED_AUTH)
    except CartContextMissing as exc:
        return Spoke(_no_context(exc))
    except McpError:
        return Spoke(SILPO_DOWN)

    resolved = next((ln for ln in cart.lines if not ln.unavailable), None)
    if resolved is None:
        return Spoke(NOT_A_DEAL, toast="Нема в наявності")
    if resolved.old_price is not None and resolved.old_price > resolved.unit_price:
        resolved = resolved.model_copy(
            update={
                "reason_text": DEAL_REASON.format(
                    price=uah(resolved.unit_price), old=uah(resolved.old_price)
                )
            }
        )

    active = await services.baskets.get_active(telegram_id)
    if active is not None:
        await services.baskets.append_item(active.id, resolved)
        return await _edited_outcome(
            services, telegram_id, active.id, active.title, toast=f"Додано {resolved.name}"[:200]
        )
    cart = cart.model_copy(update={"lines": [resolved]})
    cart = apply_savings(cart.model_copy(update={"savings_notes": []}))
    await services.conversations.append(telegram_id, "assistant", draft_recap(DEALS_TITLE, cart))
    basket_id = await services.baskets.create_from_cart(telegram_id, DEALS_TITLE, "deals", cart)
    return await _draft_ready(
        services, telegram_id, basket_id, DEALS_TITLE, cart, toast=f"Додано {resolved.name}"[:200]
    )


# --- The weekly digest (Plan 4 Task 4) --------------------------------------------

DIGEST_KIND = "digest"
DIGEST_ON_OFF = frozenset({"on", "off", "так", "ні", "увімкнути", "вимкнути"})
DIGEST_HOUR = 18
"""Kyiv, Sunday. The tick is hourly, so the message goes out in the 18:00 hour."""


async def on_digest(services: Services, telegram_id: int, argument: str) -> Outcome:
    await services.users.ensure(telegram_id)
    word = argument.strip().lower()
    if word in ("on", "так", "увімкнути"):
        await services.users.set_digest(telegram_id, True)
        return Spoke(DIGEST_ON)
    if word in ("off", "ні", "вимкнути"):
        await services.users.set_digest(telegram_id, False)
        return Spoke(DIGEST_OFF)
    user = await services.users.get(telegram_id)
    state = "Зараз увімкнено." if user is not None and user.digest_weekly else "Зараз вимкнено."
    return Spoke(f"{state}\n\n{DIGEST_HELP}")


def digest_week(now: datetime) -> tuple[date, date]:
    """Monday to the next Monday (exclusive), Kyiv, for the week `now` falls in."""
    today = now.astimezone(KYIV).date()
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=7)


def digest_due(user: User | None, now: datetime) -> bool:
    kyiv = now.astimezone(KYIV)
    return bool(
        user is not None and user.digest_weekly and kyiv.weekday() == 6 and kyiv.hour >= DIGEST_HOUR
    )


async def digest_for(
    services: Services,
    telegram_id: int,
    *,
    now: datetime | None = None,
    mcp: SilpoClient | None = None,
) -> Spoke | None:
    """This week's digest from stored data, or nothing. `mcp` is optional and only
    adds the expiring coupons; without it that section is simply absent."""
    stores = services.habits
    if stores is None:
        return None
    now = now or datetime.now(UTC)
    start, end = digest_week(now)
    start_at = datetime.combine(start, datetime.min.time(), tzinfo=KYIV).astimezone(UTC)
    end_at = datetime.combine(end, datetime.min.time(), tzinfo=KYIV).astimezone(UTC)

    online = [
        e
        for e in await stores.purchases.events(telegram_id)
        if e.source == "online" and start_at <= e.bought_at < end_at
    ]
    online_spent = sum((e.unit_price * Decimal(str(e.qty)) for e in online), Decimal("0")).quantize(
        Decimal("0.01")
    )
    receipts = (
        await stores.receipts.between(telegram_id, start_at, end_at)
        if stores.receipts is not None
        else []
    )
    user = await services.users.get(telegram_id)
    today = now.astimezone(KYIV).date()
    horizon = today + timedelta(days=7)
    habits = await stores.habits.list(telegram_id)
    due_soon = [h for h in due_to_buy(habits, horizon) if h.due_on > today or h.is_due(today)]
    expiring: list[ExpiringCoupon] = []
    if mcp is not None:
        try:
            for coupon in _listed(await mcp.get_my_coupons(), "coupons", "items", "data"):
                if not isinstance(coupon, dict) or not coupon_usable(coupon):
                    continue
                ends = _date_of(coupon.get("endDate"))
                if ends is None or not (today <= ends <= horizon):
                    continue
                [note] = describe_coupons([coupon]) or [""]
                if note:
                    expiring.append(ExpiringCoupon(text=note, ends_on=ends))
        except Exception:
            log.info("coupons unavailable for the digest of %s", telegram_id, exc_info=True)

    text = digest_text(
        DigestInput(
            week_start=start,
            week_end=end,
            online_spent=online_spent,
            online_lines=len(online),
            receipts=receipts,
            budget_cap=user.budget_weekly if user else None,
            due_next_week=due_soon,
            expiring=expiring,
        ),
        today,
    )
    return Spoke(text) if text else None


def _date_of(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except TypeError, ValueError:
        return None


async def send_digest_if_due(
    services: Services, telegram_id: int, *, now: datetime, mcp: SilpoClient | None = None
) -> bool:
    """Sunday evening, once per ISO week, only to a subscriber. Returns whether sent."""
    stores = services.habits
    if stores is None:
        return False
    user = await services.users.get(telegram_id)
    if not digest_due(user, now):
        return False
    week = (
        f"{now.astimezone(KYIV).isocalendar().year}-W{now.astimezone(KYIV).isocalendar().week:02d}"
    )
    if await stores.notifications.last_sent(telegram_id, DIGEST_KIND, week) is not None:
        return False
    digest = await digest_for(services, telegram_id, now=now, mcp=mcp)
    if digest is None:
        return False
    await stores.notifications.record(telegram_id, DIGEST_KIND, [week], at=now)
    await services.notify(telegram_id, digest)
    return True


async def _schedule_receipts(services: Services, telegram_id: int, context: SearchContext) -> None:
    stores = services.habits
    if stores is None or telegram_id in stores.receipts_in_flight:
        return
    last = await stores.imports.last_ok(telegram_id, "offline")
    if last is not None and datetime.now(UTC) - last < RECEIPTS_EVERY:
        return
    stores.receipts_in_flight.add(telegram_id)
    services.spawn(refresh_receipts(services, telegram_id, context))


async def refresh_receipts(services: Services, telegram_id: int, context: SearchContext) -> None:
    """The after-turn import: receipts only, with the context the turn already read.

    Never raises — nobody is awaiting it. Every outcome is a row in `history_imports`.
    If this is the first time receipts could be read, the learning payoff that
    `on_linked` held back goes out now (`send_payoff`).
    """
    stores = services.habits
    if stores is None:
        return
    try:
        connect = stores.connect_background
        session = (
            connect(telegram_id, wait_seconds=AFTER_TURN_WAIT)
            if connect is not None
            else services.connect(telegram_id)
        )
        async with session as mcp:
            await refresh_history(services, telegram_id, mcp, context=context, online=False)
        await send_payoff(services, telegram_id)
    except Busy:
        await stores.imports.record(telegram_id, "offline", "skipped", "a turn held the session")
    except (NotAuthenticated, McpError) as exc:
        await stores.imports.record(
            telegram_id, "offline", "failed", f"{type(exc).__name__}: {exc}"
        )
    except Exception:
        log.exception("after-turn receipts import failed for %s", telegram_id)
    finally:
        stores.receipts_in_flight.discard(telegram_id)


async def refresh_history(
    services: Services,
    telegram_id: int,
    mcp: SilpoClient,
    *,
    now: datetime | None = None,
    context: SearchContext | None = None,
    online: bool = True,
) -> ImportReport:
    """Read what is new, record each outcome, recompute habits.

    Receipts need the cart's context; with none they are *skipped* and the skip is a
    row in `history_imports`, not a log line. A failed source is recorded the same
    way. Habits are recomputed from every stored purchase — never edited in place.

    `context` is the one a turn already read, so the cart is not read twice;
    `online=False` reads receipts alone (`refresh_receipts`).
    """
    stores = services.habits
    if stores is None:
        raise RuntimeError("habits are not configured")
    now = now or datetime.now(UTC)

    if context is None:
        try:
            _, context = await load_context(mcp, check_slot=False)
        except Exception as exc:
            # A cart that cannot be read is a cart with no context: receipts are
            # skipped and the online source is still read. The reason is recorded
            # with the skip.
            log.info("no cart context for %s: %s", telegram_id, exc)

    report = await import_history(
        mcp,
        stores.purchases,
        telegram_id,
        context=context,
        since_online=await stores.imports.last_ok(telegram_id, "online"),
        since_offline=await stores.imports.last_ok(telegram_id, "offline"),
        now=now,
        include_online=online,
        receipts=stores.receipts,
    )
    failed = {e.partition(":")[0] for e in report.errors}
    if online:
        await stores.imports.record(
            telegram_id,
            "online",
            "failed" if "online" in failed else "ok",
            next((e for e in report.errors if e.startswith("online")), ""),
            at=now,
        )
    if report.skipped is not None:
        await stores.imports.record(telegram_id, "offline", "skipped", report.skipped, at=now)
    else:
        await stores.imports.record(
            telegram_id,
            "offline",
            "failed" if "offline" in failed else "ok",
            next((e for e in report.errors if e.startswith("offline")), ""),
            at=now,
        )

    habits = compute_habits(await stores.purchases.events(telegram_id))
    await stores.habits.replace(telegram_id, habits)
    return report


async def on_linked(services: Services, telegram_id: int) -> Outcome | None:
    """J2's learning payoff, after the account is linked: a backfill, then one message
    naming the shopping rhythm — or **nothing**, when nothing passes the threshold.

    `None` means say nothing. Not «поки що нема звичок»: a person who just linked has
    been promised a grocery agent, not an analysis of their loyalty card.
    """
    stores = _habits_or_off(services)
    if isinstance(stores, Spoke):
        return None
    try:
        async with services.connect(telegram_id) as mcp:
            report = await refresh_history(services, telegram_id, mcp)
    except NotAuthenticated, CartContextMissing, McpError:
        log.info("backfill after link failed for %s", telegram_id, exc_info=True)
        return None
    if report.offline is None:
        # Receipts were skipped — most often a cart with no live timeslot — or failed.
        # They are where most shopping is, so a payoff now would be built from online
        # orders alone and say «ваші покупки вже в Коморі» about half of them. Held
        # back instead: the first turn that reads a cart context imports receipts and
        # sends it then (`refresh_receipts` → `send_payoff`).
        return None
    return await _payoff(stores, telegram_id)


async def _payoff(stores: HabitServices, telegram_id: int) -> Spoke | None:
    """The payoff, at most once per user — and recorded as sent before it is said."""
    if await stores.notifications.last_sent(telegram_id, PAYOFF_KIND, PAYOFF_SUBJECT):
        return None
    habits = [h for h in await stores.habits.list(telegram_id) if h.reorderable]
    if not habits:
        return None
    await stores.notifications.record(telegram_id, PAYOFF_KIND, [PAYOFF_SUBJECT])
    return Spoke(payoff_text(habits, today_in_kyiv()))


async def send_payoff(services: Services, telegram_id: int) -> None:
    """Say the payoff `on_linked` could not, if there is one to say."""
    if services.habits is None:
        return
    payoff = await _payoff(services.habits, telegram_id)
    if payoff is not None:
        await services.notify(telegram_id, payoff)


def payoff_text(habits: list[Habit], today: date) -> str:
    """«Відстежую 3 позиції» reads well at three and is never sent at zero.

    It names no span. «За останні 19 місяців» was counted from the oldest online order,
    while receipts — where the shopping is — reached back eighty days: a number true of
    one source, read as a claim about all of them."""
    count = f"{len(habits)} {pl(len(habits), 'позицію', 'позиції', 'позицій')}"
    intro = PAYOFF_INTRO.format(items=count)
    named = [
        f"• {h.name} — кожні ~{days(round(h.median_gap_days))}" for h in habits[:MAX_NUDGE_ITEMS]
    ]
    return "\n".join([intro, *named, "", PAYOFF_OUTRO])


async def on_usual(services: Services, telegram_id: int) -> Outcome:
    """«/usual» — what Komora tracks, sentences from the engine, mute toggles."""
    stores = _habits_or_off(services)
    if isinstance(stores, Spoke):
        return stores
    await services.users.ensure(telegram_id)
    blob, _ = await services.users.get_token_blob(telegram_id)
    if not blob:
        return _needs_link(WELCOME)
    habits = await stores.habits.list(telegram_id)
    return HabitsReady(
        habits=habits, today=today_in_kyiv(), fresh_at=await _fresh_at(stores, telegram_id)
    )


async def on_mute_list(services: Services, telegram_id: int) -> Outcome:
    """«/mute» — what is muted, with the way back on."""
    stores = _habits_or_off(services)
    if isinstance(stores, Spoke):
        return stores
    muted = [h for h in await stores.habits.list(telegram_id) if h.muted]
    if not muted:
        return Spoke(NOTHING_MUTED)
    return HabitsReady(
        habits=muted, today=today_in_kyiv(), fresh_at=await _fresh_at(stores, telegram_id)
    )


async def on_set_mute(
    services: Services, telegram_id: int, product_key: str, *, muted: bool
) -> Outcome:
    """Mute or unmute one habit. The key comes from the client; it is looked up for the
    authenticated sender only, so a guessed key mutes nothing of anyone else's."""
    stores = _habits_or_off(services)
    if isinstance(stores, Spoke):
        return stores
    key = product_key.strip()
    if not key or len(key) > MAX_PRODUCT_KEY:
        return Spoke(UNKNOWN_HABIT, toast=UNKNOWN_HABIT)
    if not await stores.habits.set_muted(telegram_id, key, muted):
        return Spoke(UNKNOWN_HABIT, toast=UNKNOWN_HABIT)
    return HabitsReady(
        habits=await stores.habits.list(telegram_id),
        today=today_in_kyiv(),
        fresh_at=await _fresh_at(stores, telegram_id),
        toast=MUTED_TOAST if muted else UNMUTED_TOAST,
    )


async def on_habits_draft(
    services: Services,
    telegram_id: int,
    *,
    from_nudge: bool = False,
    keys: Sequence[str] | None = None,
    discounted_only: bool = False,
) -> Outcome:
    """A basket from due habits — no model request — through the known-product resolve
    path and the ordinary pipeline, then confirmed like any other draft.

    From a nudge, the draft holds what the nudge named: due and nudgeable. From «your
    usual», every tracked habit due by date (`due_to_buy`) — the list was opened on
    purpose, so the nudge tier does not apply. With none due, everything tracked: the
    user asked for a basket, and «нема з чого» is the only honest refusal.

    `keys` (a deal alert's «Зібрати кошик») names the products exactly;
    `discounted_only` («/deals») takes the tracked products currently on promotion,
    per the last scan. Both are the user's own habits, looked up for the sender.
    """
    stores = _habits_or_off(services)
    if isinstance(stores, Spoke):
        return stores
    await services.users.ensure(telegram_id)
    today = today_in_kyiv()
    tracked = await stores.habits.list(telegram_id)
    usable = [h for h in tracked if h.reorderable and not h.muted]
    if keys is not None:
        wanted = set(keys)
        chosen = [h for h in usable if h.product_key in wanted]
    elif discounted_only:
        chosen = usable  # narrowed to the branch's snapshots once the context is known
    else:
        chosen = (due(tracked, today) if from_nudge else []) or due_to_buy(tracked, today) or usable
    lines = habit_lines(chosen, today)
    if not lines:
        return Spoke(NO_HABITS_TO_BUILD)

    user = await services.users.get(telegram_id)
    budget_cap = user.budget_weekly if user else None
    try:
        async with services.connect(telegram_id) as mcp:
            _, context = await _load_context(services, telegram_id, mcp)
            if discounted_only:
                # Snapshots are per branch, and the branch is only known from the cart.
                deals = await _tracked_deals(services, telegram_id, branch_id=context.branch_id)
                on_sale = {d.habit.product_key for d in deals}
                lines = [ln for ln in lines if ln.product_id in on_sale]
                if not lines:
                    return Spoke(NO_HABITS_TO_BUILD)
            cart, learned = await build_known_cart(lines, mcp, context, budget_cap=budget_cap)
    except NotAuthenticated:
        return _needs_link(NEED_AUTH)
    except CartContextMissing as exc:
        return Spoke(_no_context(exc))
    except McpError:
        return Spoke(SILPO_DOWN)

    for product_key, article in learned.items():
        await stores.purchases.learn_external_id(telegram_id, product_key, article)

    await services.conversations.append(telegram_id, "assistant", draft_recap(HABITS_TITLE, cart))
    if not cart.lines:
        return DraftReady(title=HABITS_TITLE, cart=cart, budget_cap=budget_cap)
    basket_id = await services.baskets.create_from_cart(
        telegram_id, HABITS_TITLE, HABITS_INTENT, cart
    )
    return await _draft_ready(services, telegram_id, basket_id, HABITS_TITLE, cart)


async def on_delete(services: Services, telegram_id: int) -> Outcome:
    """«/delete» asks first. The wipe is one tap away and cannot be undone."""
    if await services.users.get(telegram_id) is None:
        return Spoke(NOTHING_TO_DELETE)
    return Ask(DELETE_ASK, yes="delete:confirm", yes_label=DELETE_YES, no="delete:keep")


async def on_delete_confirm(services: Services, telegram_id: int) -> Outcome:
    if not await services.users.delete(telegram_id):
        return Spoke(NOTHING_TO_DELETE, toast="Нічого видаляти")
    return Spoke(DELETED, toast="Видалено")


IN_CART_MIN = timedelta(days=3)


def already_in_cart(habit: Habit, sent_at: datetime | None, now: datetime) -> bool:
    """Komora put this product in the Silpo cart after the last purchase it has seen.

    A purchase is recorded only once an order is *received*, so a habits draft pushed
    minutes ago is invisible to the engine — and the first job tick after the live walk
    would have said «Схоже, закінчуються» about a cheese and a bun sitting in the very
    cart Komora had just filled. `synced_at` is exact about the first half. The window
    (one interval, at least three days) is the other half: a line the user took out of
    the cart and never ordered must not silence the product for ever.
    """
    if sent_at is None:
        return False
    after_last_purchase = sent_at.astimezone(KYIV).date() >= habit.last_bought
    window = max(IN_CART_MIN, timedelta(days=habit.median_gap_days))
    return after_last_purchase and now - sent_at < window


DEFAULT_QUIET_HOURS = (22, 8)
"""Kyiv hours between which nothing is sent unasked, unless the user set their own."""


def in_quiet_hours(user: User | None, now: datetime) -> bool:
    start, end = DEFAULT_QUIET_HOURS
    if user is not None and user.quiet_from is not None and user.quiet_to is not None:
        start, end = user.quiet_from, user.quiet_to
    hour = now.astimezone(KYIV).hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


async def nudge_for(
    services: Services, telegram_id: int, *, now: datetime | None = None
) -> NudgeReady | None:
    """The proactive message, or nothing: due, nudgeable, not muted, not nudged about
    inside the cooldown, and not during quiet hours. Records what it is about to say."""
    stores = services.habits
    if stores is None:
        return None
    now = now or datetime.now(UTC)
    if in_quiet_hours(await services.users.get(telegram_id), now):
        return None
    today = now.astimezone(KYIV).date()
    candidates = due(await stores.habits.list(telegram_id), today)
    fresh: list[Habit] = []
    in_cart = await services.baskets.synced_at(telegram_id)
    for habit in candidates:
        if already_in_cart(habit, in_cart.get(habit.product_key), now):
            continue
        last = await stores.notifications.last_sent(telegram_id, NUDGE_KIND, habit.product_key)
        # Once per expected purchase, not once per cooldown. `due_on` moves only when a
        # purchase is recorded, so a nudge sent on or after it has already asked about
        # this purchase; asking again every three days until the habit lapsed was
        # nagging about a question the user had answered by not tapping.
        if last is None or (
            now - last >= NUDGE_COOLDOWN and last.astimezone(KYIV).date() < habit.due_on
        ):
            fresh.append(habit)
    if not fresh:
        return None
    chosen = fresh[:MAX_NUDGE_ITEMS]
    await stores.notifications.record(
        telegram_id, NUDGE_KIND, [h.product_key for h in chosen], at=now
    )
    return NudgeReady(habits=chosen, today=today)
