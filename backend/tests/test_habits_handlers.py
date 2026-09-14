"""The habits conversation end to end: repos, handlers, the job, the rendering, the API.

Driven against a real in-memory database, the Silpo fake and a recording `notify`,
so the whole loop — link, backfill, payoff, «/usual», mute, nudge, habits draft,
«/delete» — runs without Telegram, a model or the network.
"""

import contextlib
from collections.abc import AsyncIterator, Coroutine
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from komora.api.app import create_app
from komora.bot.habits_job import REFRESH_EVERY, refresh_if_stale, tick
from komora.bot.handlers import (
    DELETE_KEPT,
    DELETED,
    DISMISSED,
    HABITS_OFF,
    NO_HABITS_TO_BUILD,
    NOTHING_MUTED,
    NUDGE_KIND,
    SLOT_EXPIRED,
    UNKNOWN_HABIT,
    HabitServices,
    Services,
    in_quiet_hours,
    nudge_for,
    on_callback,
    on_delete,
    on_habits_draft,
    on_linked,
    on_mute_list,
    on_push,
    on_set_mute,
    on_start,
    on_usual,
    refresh_history,
    refresh_receipts,
    send_payoff,
    today_in_kyiv,
)
from komora.bot.outcomes import Ask, DraftReady, HabitsReady, NudgeReady, Outcome, Spoke, Synced
from komora.bot.render import NO_HABITS, to_reply
from komora.core.habits.engine import compute_habits
from komora.core.habits.purchases import PurchaseEvent, offline_purchases
from komora.core.mcp.auth import AuthorizationBridge
from komora.core.mcp.gateway import Busy
from komora.db.repo import (
    BasketRepo,
    ConversationRepo,
    HabitRepo,
    HistoryImportRepo,
    NotificationRepo,
    PurchaseRepo,
    UserRepo,
)
from komora.db.tables import User
from tests.fakes import CONTEXT, INITDATA_TOKEN, FakeSilpo, product, signed_init_data

USER = 4242
OTHER = 777
MILK = "1ed07604-a3ca-624e-87ee-dd63763181f9"
BREAD = "1ed0765c-17fd-69fc-8ff2-dd63763181f9"


def receipt(i: int, day: date, *products: tuple[str, str, int]) -> dict:
    return {
        "createdAt": f"{day.isoformat()}T13:00:00",
        "filId": 1,
        "receiptUrl": f"https://receipt.silpo.elkasa.com.ua/T{i}",
        "products": [
            {
                "lagerId": article,
                "name": name,
                "price": 40.0,
                "quantity": 1,
                "unit": "900г",
                "catalogProduct": {"id": pid, "weighted": False},
            }
            for pid, name, article in products
        ],
    }


def weekly_receipts(weeks: int = 8, start: date = date(2026, 6, 1)) -> list[dict]:
    """Milk every week, bread every other week — eight and four events."""
    out = []
    for i in range(weeks):
        day = start + timedelta(days=7 * i)
        lines = [(MILK, "Молоко Галичина", 815253)]
        if i % 2 == 0:
            lines.append((BREAD, "Хліб Французький", 878840))
        out.append(receipt(i, day, *lines))
    return out


CATALOGUE = {
    "815253": [product("Молоко Галичина", 42.9, product_id=MILK)],
    "878840": [product("Хліб Французький", 28.5, product_id=BREAD)],
}


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[int, Outcome]] = []

    async def __call__(self, telegram_id: int, outcome: Outcome) -> None:
        self.sent.append((telegram_id, outcome))


class Deferred:
    """A `Spawn` that holds work until the test says «the reply has gone out»."""

    def __init__(self) -> None:
        self.pending: list[Coroutine[Any, Any, None]] = []

    def __call__(self, work: Coroutine[Any, Any, None]) -> None:
        self.pending.append(work)

    async def drain(self) -> int:
        ran = 0
        while self.pending:
            await self.pending.pop(0)
            ran += 1
        return ran


def build(
    sessions: async_sessionmaker,
    *,
    silpo: FakeSilpo | None = None,
    busy: bool = False,
    habits: bool = True,
) -> tuple[Services, FakeSilpo, Recorder]:
    fake = silpo or FakeSilpo(CATALOGUE, offline_orders=weekly_receipts())
    notify = Recorder()

    @contextlib.asynccontextmanager
    async def connect(telegram_id: int) -> AsyncIterator[FakeSilpo]:
        yield fake

    @contextlib.asynccontextmanager
    async def connect_background(
        telegram_id: int, *, wait_seconds: float = 2.0
    ) -> AsyncIterator[FakeSilpo]:
        if busy:
            raise Busy("turn in flight")
        yield fake

    async def no_tools(mcp: FakeSilpo) -> list:
        return []

    stores = (
        HabitServices(
            purchases=PurchaseRepo(sessions),
            habits=HabitRepo(sessions),
            imports=HistoryImportRepo(sessions),
            notifications=NotificationRepo(sessions),
            connect_background=connect_background,  # type: ignore[arg-type]
        )
        if habits
        else None
    )
    services = Services(
        users=UserRepo(sessions),
        conversations=ConversationRepo(sessions),
        baskets=BasketRepo(sessions),
        llm=None,  # type: ignore[arg-type]
        tools=no_tools,  # type: ignore[arg-type]
        connect=connect,  # type: ignore[arg-type]
        habits=stores,
        notify=notify,
        spawn=Deferred(),
    )
    return services, fake, notify


def deferred(services: Services) -> Deferred:
    assert isinstance(services.spawn, Deferred)
    return services.spawn


async def linked(services: Services, telegram_id: int = USER) -> None:
    await services.users.ensure(telegram_id)
    await services.users.set_token_blob(telegram_id, b"tokens", datetime.now(UTC))


# --- repositories ------------------------------------------------------------------


class TestRepos:
    async def test_upsert_is_idempotent_and_refreshes(self, sessions) -> None:  # type: ignore[no-untyped-def]
        repo = PurchaseRepo(sessions)
        await UserRepo(sessions).ensure(USER)
        event = PurchaseEvent(
            source="offline",
            receipt_key="r:1",
            product_key=MILK,
            name="Молоко",
            qty=1,
            unit="900г",
            unit_price=Decimal("40.00"),
            weighted=False,
            reorderable=True,
            bought_at=datetime(2026, 6, 1, 10, tzinfo=UTC),
        )
        assert await repo.upsert(USER, [event]) == 1
        again = PurchaseEvent(**{**event.__dict__, "qty": 3, "external_product_id": 815253})
        assert await repo.upsert(USER, [again, event]) == 2
        (stored,) = await repo.events(USER)
        assert stored.qty == 1  # the last write wins, never a sum
        assert stored.external_product_id == 815253  # learned once, never unlearned
        assert stored.bought_at.tzinfo is not None

    async def test_habits_are_replaced_but_mutes_survive(self, sessions) -> None:  # type: ignore[no-untyped-def]
        await UserRepo(sessions).ensure(USER)
        purchases, habits = PurchaseRepo(sessions), HabitRepo(sessions)
        silpo = FakeSilpo(offline_orders=weekly_receipts())
        from komora.core.habits.purchases import offline_purchases

        await purchases.upsert(USER, offline_purchases(await silpo.get_my_offline_orders(None)))  # type: ignore[arg-type]
        computed = compute_habits(await purchases.events(USER))
        await habits.replace(USER, computed)
        assert await habits.set_muted(USER, MILK, True)
        assert not await habits.set_muted(USER, "not-a-habit", True)
        assert not await habits.set_muted(OTHER, MILK, True)  # someone else's key
        await habits.replace(USER, computed)  # a recompute
        by_key = {h.product_key: h for h in await habits.list(USER)}
        assert by_key[MILK].muted and not by_key[BREAD].muted

    async def test_linked_and_delete(self, sessions) -> None:  # type: ignore[no-untyped-def]
        users = UserRepo(sessions)
        await users.ensure(USER)
        await users.ensure(OTHER)
        await users.set_token_blob(USER, b"t", datetime.now(UTC))
        assert await users.linked() == [USER]
        await HistoryImportRepo(sessions).record(USER, "online", "ok")
        await NotificationRepo(sessions).record(USER, NUDGE_KIND, [MILK])
        assert await users.delete(USER)
        assert not await users.delete(USER)
        assert await users.get(USER) is None
        assert await HistoryImportRepo(sessions).last_ok(USER, "online") is None
        assert await NotificationRepo(sessions).last_sent(USER, NUDGE_KIND, MILK) is None
        assert await users.get(OTHER) is not None

    async def test_import_freshness_is_the_last_ok_only(self, sessions) -> None:  # type: ignore[no-untyped-def]
        await UserRepo(sessions).ensure(USER)
        imports = HistoryImportRepo(sessions)
        assert await imports.last_ok(USER, "online") is None
        await imports.record(USER, "online", "ok")
        first = await imports.last_ok(USER, "online")
        await imports.record(USER, "online", "failed", "boom")
        await imports.record(USER, "offline", "skipped", "no context")
        assert await imports.last_ok(USER, "online") == first
        assert await imports.last_ok(USER, "offline") is None


# --- refresh, payoff, «/usual» ----------------------------------------------------


class TestRefreshAndPayoff:
    async def test_refresh_records_both_sources_and_computes_habits(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        report = await refresh_history(services, USER, silpo)
        assert report.ok and report.online == 0 and report.offline
        stores = services.habits
        assert stores is not None
        assert await stores.imports.last_ok(USER, "online") is not None
        assert await stores.imports.last_ok(USER, "offline") is not None
        keys = {h.product_key for h in await stores.habits.list(USER)}
        assert keys == {MILK, BREAD}

    async def test_no_cart_context_skips_receipts_and_records_the_skip(self, sessions) -> None:  # type: ignore[no-untyped-def]
        silpo = FakeSilpo(
            CATALOGUE, offline_orders=weekly_receipts(), fails={"get_my_shopping_cart"}
        )
        services, silpo, _ = build(sessions, silpo=silpo)
        await linked(services)
        report = await refresh_history(services, USER, silpo)
        assert report.skipped and report.offline is None
        stores = services.habits
        assert stores is not None
        assert await stores.imports.last_ok(USER, "offline") is None
        assert await stores.habits.list(USER) == []

    async def test_the_payoff_names_the_rhythm_or_says_nothing(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, _, _ = build(sessions)
        await linked(services)
        payoff = await on_linked(services, USER)
        assert isinstance(payoff, Spoke)
        assert "Відстежую 2 позиції" in payoff.text
        assert "Молоко Галичина — кожні ~7 днів" in payoff.text
        # No span: «за останні 19 місяців» counted the oldest online order.
        assert "місяц" not in payoff.text

        empty, _, _ = build(sessions, silpo=FakeSilpo(CATALOGUE))
        await linked(empty, OTHER)
        assert await on_linked(empty, OTHER) is None

    async def test_usual_needs_a_link_then_lists_with_sentences(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        unlinked = await on_usual(services, USER)
        assert isinstance(unlinked, Spoke) and unlinked.needs_link

        await linked(services)
        await refresh_history(services, USER, silpo)
        outcome = await on_usual(services, USER)
        assert isinstance(outcome, HabitsReady)
        assert outcome.fresh_at is not None
        milk = next(h for h in outcome.habits if h.product_key == MILK)
        reply = to_reply(outcome)
        assert milk.sentence(outcome.today) in reply.text
        assert "Зібрати кошик" in [b.label for b in reply.buttons]
        assert f"mute:{MILK}" in [b.data for b in reply.buttons]

    async def test_usual_with_nothing_tracked_is_honest(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, _, _ = build(sessions, silpo=FakeSilpo(CATALOGUE))
        await linked(services)
        outcome = await on_usual(services, USER)
        assert isinstance(outcome, HabitsReady) and outcome.habits == []
        assert to_reply(outcome).text == NO_HABITS
        assert to_reply(outcome).buttons == ()

    async def test_habits_off_answers_rather_than_crashes(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, _, _ = build(sessions, habits=False)
        assert await on_usual(services, USER) == Spoke(HABITS_OFF)
        assert await on_habits_draft(services, USER) == Spoke(HABITS_OFF)
        assert await on_linked(services, USER) is None
        assert await nudge_for(services, USER) is None


# --- mute ---------------------------------------------------------------------------


class TestMute:
    async def test_mute_and_unmute_keep_the_list_and_toast(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        await refresh_history(services, USER, silpo)
        muted = await on_callback(services, USER, f"mute:{MILK}")
        assert isinstance(muted, HabitsReady) and muted.toast
        assert next(h for h in muted.habits if h.product_key == MILK).muted
        assert f"unmute:{MILK}" in [b.data for b in to_reply(muted).buttons]

        listed = await on_mute_list(services, USER)
        assert isinstance(listed, HabitsReady) and [h.product_key for h in listed.habits] == [MILK]

        back = await on_set_mute(services, USER, MILK, muted=False)
        assert isinstance(back, HabitsReady)
        assert not next(h for h in back.habits if h.product_key == MILK).muted
        assert await on_mute_list(services, USER) == Spoke(NOTHING_MUTED)

    async def test_a_guessed_or_oversized_key_mutes_nothing(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        await refresh_history(services, USER, silpo)
        assert await on_set_mute(services, OTHER, MILK, muted=True) == Spoke(
            UNKNOWN_HABIT, toast=UNKNOWN_HABIT
        )
        assert await on_set_mute(services, USER, "x" * 65, muted=True) == Spoke(
            UNKNOWN_HABIT, toast=UNKNOWN_HABIT
        )
        assert await on_set_mute(services, USER, "", muted=True) == Spoke(
            UNKNOWN_HABIT, toast=UNKNOWN_HABIT
        )


# --- the habits draft --------------------------------------------------------------


class TestHabitsDraft:
    async def test_a_draft_is_built_without_a_model_and_persisted(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        await refresh_history(services, USER, silpo)
        outcome = await on_callback(services, USER, "habits:build")
        assert isinstance(outcome, DraftReady) and outcome.basket_id is not None
        assert {ln.product_id for ln in outcome.cart.lines} == {MILK, BREAD}
        assert all(ln.reason_kind == "habit" for ln in outcome.cart.lines)
        assert all("Ви купуєте" in ln.reason_text for ln in outcome.cart.lines)
        # Searched by article number — the stored lagerId — never by name.
        assert sorted(silpo.search_calls[-1]) == ["815253", "878840"]
        basket = await services.baskets.get(outcome.basket_id)
        assert basket is not None and basket.intent == "habits"

    async def test_a_muted_habit_stays_out_and_nothing_left_is_said(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        await refresh_history(services, USER, silpo)
        await on_set_mute(services, USER, MILK, muted=True)
        outcome = await on_habits_draft(services, USER)
        assert isinstance(outcome, DraftReady)
        assert [ln.product_id for ln in outcome.cart.lines] == [BREAD]
        await on_set_mute(services, USER, BREAD, muted=True)
        assert await on_habits_draft(services, USER) == Spoke(NO_HABITS_TO_BUILD)

    async def test_a_nudge_builds_what_it_named_usual_what_is_due(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        await refresh_history(services, USER, silpo)
        stores = services.habits
        assert stores is not None
        yesterday = today_in_kyiv() - timedelta(days=1)
        by_key = {h.product_key: h for h in await stores.habits.list(USER)}
        tight = replace(by_key[MILK], due_on=yesterday, cv=0.3)
        loose = replace(by_key[BREAD], due_on=yesterday, cv=0.9)  # tracked, not nudgeable
        await stores.habits.replace(USER, [tight, loose])

        nudged = await on_callback(services, USER, "habits:nudge")
        assert isinstance(nudged, DraftReady)
        assert [ln.product_id for ln in nudged.cart.lines] == [MILK]

        usual = await on_callback(services, USER, "habits:build")
        assert isinstance(usual, DraftReady)
        assert {ln.product_id for ln in usual.cart.lines} == {MILK, BREAD}

        # Nothing due: a draft the user asked for still holds everything tracked.
        later = today_in_kyiv() + timedelta(days=5)
        await stores.habits.replace(
            USER, [replace(tight, due_on=later), replace(loose, due_on=later)]
        )
        idle = await on_habits_draft(services, USER)
        assert isinstance(idle, DraftReady)
        assert {ln.product_id for ln in idle.cart.lines} == {MILK, BREAD}

    async def test_an_expired_slot_refuses_in_one_sentence(self, sessions) -> None:  # type: ignore[no-untyped-def]
        passed = {
            "start": CONTEXT.timeslot_start,
            "end": CONTEXT.timeslot_end,
            "available": False,
            "deliveryType": CONTEXT.delivery_type,
        }
        silpo = FakeSilpo(CATALOGUE, offline_orders=weekly_receipts(), slots=[passed])
        services, silpo, _ = build(sessions, silpo=silpo)
        await linked(services)
        await seeded(services)
        # Not a draft of «Не знайшлося» lines for goods Silpo stocks.
        assert await on_habits_draft(services, USER) == Spoke(SLOT_EXPIRED)
        assert silpo.search_calls == []
        # Receipts need no live slot, so the turn still keeps history current.
        assert len(deferred(services).pending) == 1

    async def test_a_name_search_teaches_the_article_number(self, sessions) -> None:  # type: ignore[no-untyped-def]
        """An online-only product carries no article; the draft learns it on the way."""
        order = {
            "orderId": "o1",
            "status": "received",
            "createdAt": "2026-06-01T10:00:00+00:00",
            "delivery": {"deliveredAt": "2026-06-01T12:00:00+00:00"},
            "products": [
                {
                    "id": MILK,
                    "name": "Молоко Галичина",
                    "price": 40,
                    "quantity": 1,
                    "removed": False,
                }
            ],
        }
        orders = [
            {
                **order,
                "orderId": f"o{i}",
                "delivery": {"deliveredAt": f"2026-06-{1 + 7 * i:02d}T12:00:00+00:00"},
            }
            for i in range(4)
        ]
        found = product("Молоко Галичина", 42.9, product_id=MILK)
        found["externalProductId"] = 815253
        silpo = FakeSilpo({"Молоко Галичина": [found]}, online_orders=orders)
        services, silpo, _ = build(sessions, silpo=silpo)
        await linked(services)
        await refresh_history(services, USER, silpo)
        outcome = await on_habits_draft(services, USER)
        assert isinstance(outcome, DraftReady)
        assert silpo.search_calls[-1] == ["Молоко Галичина"]
        stores = services.habits
        assert stores is not None
        (habit,) = await stores.habits.list(USER)
        assert habit.external_product_id == 815253


# --- nudges and the job ------------------------------------------------------------


class TestNudges:
    async def test_a_nudge_asks_once_per_expected_purchase(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        await refresh_history(services, USER, silpo)
        now = datetime(2026, 8, 1, 12, tzinfo=UTC)  # a few days past both due dates
        nudge = await nudge_for(services, USER, now=now)
        assert isinstance(nudge, NudgeReady)
        assert {h.product_key for h in nudge.habits} == {MILK, BREAD}
        reply = to_reply(nudge)
        assert "Схоже, закінчуються" in reply.text
        # Numbered, so «🔇 1» and «🔇 2» name something the user can read.
        assert "1. " in reply.text and "2. " in reply.text
        assert [b.data for b in reply.buttons][:2] == ["habits:nudge", "dismiss"]
        assert await nudge_for(services, USER, now=now + timedelta(days=1)) is None
        # Past the cooldown, still the same purchase: an ignored nudge is an answer.
        assert await nudge_for(services, USER, now=now + timedelta(days=4)) is None

        # Milk is bought again; its next expected purchase is a new question.
        stores = services.habits
        assert stores is not None
        bought = receipt(99, date(2026, 8, 2), (MILK, "Молоко Галичина", 815253))
        await stores.purchases.upsert(USER, offline_purchases({"orders": [bought]}))
        await stores.habits.replace(USER, compute_habits(await stores.purchases.events(USER)))
        again = await nudge_for(services, USER, now=datetime(2026, 8, 9, 12, tzinfo=UTC))
        assert isinstance(again, NudgeReady)
        assert [h.product_key for h in again.habits] == [MILK]

    async def test_no_nudge_about_what_komora_just_put_in_the_cart(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        await services.users.set_quiet_hours(USER, 5, 5)  # never quiet: the clock is real
        await refresh_history(services, USER, silpo)
        stores = services.habits
        assert stores is not None
        yesterday = today_in_kyiv() - timedelta(days=1)
        await stores.habits.replace(
            USER, [replace(h, due_on=yesterday) for h in await stores.habits.list(USER)]
        )
        draft = await on_habits_draft(services, USER)
        assert isinstance(draft, DraftReady) and draft.basket_id is not None
        assert isinstance(await on_push(services, USER, draft.basket_id), Synced)
        assert set(await services.baskets.synced_at(USER)) == {MILK, BREAD}

        # Minutes later the job asks: both are in the cart Komora just filled.
        assert await nudge_for(services, USER, now=datetime.now(UTC)) is None
        # A line taken out and never ordered stops silencing it after one interval of
        # its own: milk (every ~7 days) speaks again at ten days, still short of
        # lapsing; bread (every ~14) is still held back.
        later = await nudge_for(services, USER, now=datetime.now(UTC) + timedelta(days=10))
        assert isinstance(later, NudgeReady)
        assert {h.product_key for h in later.habits} == {MILK}

    async def test_a_lapsed_habit_is_never_nudged(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        await refresh_history(services, USER, silpo)
        # Weekly milk last seen in July is not «running out» in September.
        assert await nudge_for(services, USER, now=datetime(2026, 9, 14, 12, tzinfo=UTC)) is None

    async def test_quiet_hours_and_mutes_hold_a_nudge_back(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        await refresh_history(services, USER, silpo)
        night = datetime(2026, 8, 1, 20, tzinfo=UTC)  # 23:00 Kyiv
        assert await nudge_for(services, USER, now=night) is None
        await on_set_mute(services, USER, MILK, muted=True)
        day = datetime(2026, 8, 1, 12, tzinfo=UTC)
        nudge = await nudge_for(services, USER, now=day)
        assert nudge is None or MILK not in {h.product_key for h in nudge.habits}

    def test_quiet_hours_wrap_midnight_and_respect_the_user(self) -> None:
        assert in_quiet_hours(None, datetime(2026, 8, 1, 20, tzinfo=UTC))  # 23:00 Kyiv
        assert not in_quiet_hours(None, datetime(2026, 8, 1, 9, tzinfo=UTC))  # 12:00 Kyiv
        user = User(telegram_id=1, quiet_from=13, quiet_to=14)
        assert in_quiet_hours(user, datetime(2026, 8, 1, 10, 30, tzinfo=UTC))  # 13:30 Kyiv
        assert not in_quiet_hours(User(telegram_id=1, quiet_from=5, quiet_to=5), datetime.now(UTC))

    async def test_dismiss_answers_and_a_tick_refreshes_and_notifies(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, _, notify = build(sessions)
        await linked(services)
        assert await on_callback(services, USER, "dismiss") == Spoke(DISMISSED, toast="Добре")
        now = datetime(2026, 8, 1, 12, tzinfo=UTC)
        assert await tick(services, now=now) == [USER]
        assert isinstance(notify.sent[0][1], NudgeReady)
        # Fresh now: a second tick inside the window reads nothing and nudges nothing.
        assert not await refresh_if_stale(services, USER, now + timedelta(hours=1))
        assert await tick(services, now=now + timedelta(hours=1)) == []
        assert await refresh_if_stale(services, USER, now + REFRESH_EVERY + timedelta(minutes=1))

    async def test_a_busy_user_is_skipped_and_the_skip_is_recorded(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, _, _ = build(sessions, busy=True)
        await linked(services)
        assert not await refresh_if_stale(services, USER, datetime.now(UTC))
        stores = services.habits
        assert stores is not None
        assert await stores.imports.last_ok(USER, "online") is None


# --- «/delete» ----------------------------------------------------------------------


class TestDelete:
    async def test_delete_asks_then_wipes_everything(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        await refresh_history(services, USER, silpo)
        await on_habits_draft(services, USER)
        ask = await on_delete(services, USER)
        assert isinstance(ask, Ask) and ask.yes == "delete:confirm"
        assert [b.data for b in to_reply(ask).buttons] == ["delete:confirm", "delete:keep"]

        # «Скасувати» is its own answer — not the nudge's «нагадаю, коли буде пора».
        kept = await on_callback(services, USER, "delete:keep")
        assert kept == Spoke(DELETE_KEPT, toast="Нічого не видалено")
        assert await services.users.get(USER) is not None

        done = await on_callback(services, USER, "delete:confirm")
        assert done == Spoke(DELETED, toast="Видалено")
        assert await services.users.get(USER) is None
        assert await services.baskets.get_active(USER) is None
        stores = services.habits
        assert stores is not None
        assert await stores.purchases.events(USER) == []
        assert await stores.habits.list(USER) == []
        # /start afterwards is a fresh welcome, not a crash.
        assert isinstance(await on_start(services, USER), Spoke)


# --- the Mini App API --------------------------------------------------------------


def header(user_id: int) -> dict[str, str]:
    return {"Authorization": f"tma {signed_init_data(user_id)}"}


class TestApi:
    async def test_habits_routes_serialise_the_same_outcomes(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, _ = build(sessions)
        await linked(services)
        await refresh_history(services, USER, silpo)
        transport = ASGITransport(app=create_app(AuthorizationBridge(), services, INITDATA_TOKEN))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/habits")).status_code == 401

            listed = (await client.get("/api/habits", headers=header(USER))).json()
            assert listed["kind"] == "habits" and listed["fresh_at"]
            assert listed["fresh_text"].startswith("Історія оновлена")
            assert listed["empty_text"] == NO_HABITS
            assert {"due", "lapsed", "muted", "sentence"} <= set(listed["habits"][0])
            milk = next(h for h in listed["habits"] if h["product_key"] == MILK)
            assert milk["sentence"].startswith("Ви купуєте") and milk["muted"] is False

            muted = (await client.post(f"/api/habits/{MILK}/mute", headers=header(USER))).json()
            assert muted["kind"] == "habits" and muted["toast"]
            assert next(h for h in muted["habits"] if h["product_key"] == MILK)["muted"]

            foreign = (await client.post(f"/api/habits/{MILK}/mute", headers=header(OTHER))).json()
            assert foreign["kind"] == "spoke" and foreign["text"] == UNKNOWN_HABIT

            back = (await client.post(f"/api/habits/{MILK}/unmute", headers=header(USER))).json()
            assert not next(h for h in back["habits"] if h["product_key"] == MILK)["muted"]

            draft = (await client.post("/api/habits/draft", headers=header(USER))).json()
            assert draft["kind"] == "draft" and draft["basket_id"]
            assert {ln["product_id"] for ln in draft["cart"]["lines"]} == {MILK, BREAD}


# --- receipts after a turn, and the payoff they can release ------------------------


async def seeded(services: Services) -> None:
    """Habits in the database with no import ever recorded — as after a link that
    could not read receipts, or for a user who linked before receipts ran on turns."""
    stores = services.habits
    assert stores is not None
    events = offline_purchases({"orders": weekly_receipts()})
    await stores.purchases.upsert(USER, events)
    await stores.habits.replace(USER, compute_habits(await stores.purchases.events(USER)))


class TestReceiptsAfterATurn:
    async def test_a_turn_with_a_context_imports_receipts_after_its_reply(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, silpo, notify = build(sessions)
        await linked(services)
        await seeded(services)
        stores = services.habits
        assert stores is not None

        assert isinstance(await on_habits_draft(services, USER), DraftReady)
        later = deferred(services)
        # Scheduled, not run: the reply is not kept waiting for it.
        assert len(later.pending) == 1
        assert await stores.imports.last_ok(USER, "offline") is None
        # A second turn while the first import waits schedules nothing more.
        await on_habits_draft(services, USER)
        assert len(later.pending) == 1

        silpo.history_calls.clear()
        assert await later.drain() == 1
        assert await stores.imports.last_ok(USER, "offline") is not None
        # Receipts alone, with the turn's context — online orders are the job's.
        assert {source for source, _ in silpo.history_calls} == {"offline"}
        assert await stores.imports.last_ok(USER, "online") is None

        # The payoff the link never got to say goes out now, once.
        (said,) = notify.sent
        assert isinstance(said[1], Spoke) and "Відстежую 2 позиції" in said[1].text
        await send_payoff(services, USER)
        assert len(notify.sent) == 1

        # Fresh now: the next turn schedules nothing.
        await on_habits_draft(services, USER)
        assert later.pending == []

    async def test_a_link_without_receipts_holds_the_payoff_back(self, sessions) -> None:  # type: ignore[no-untyped-def]
        silpo = FakeSilpo(
            CATALOGUE, offline_orders=weekly_receipts(), fails={"get_my_shopping_cart"}
        )
        services, _, _ = build(sessions, silpo=silpo)
        await linked(services)
        assert await on_linked(services, USER) is None
        stores = services.habits
        assert stores is not None
        assert await stores.notifications.last_sent(USER, "payoff", "habits") is None

    async def test_the_payoff_at_link_is_not_said_twice(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, _, notify = build(sessions)
        await linked(services)
        assert isinstance(await on_linked(services, USER), Spoke)
        await send_payoff(services, USER)
        assert notify.sent == []

    async def test_a_busy_session_is_recorded_as_a_skip(self, sessions) -> None:  # type: ignore[no-untyped-def]
        services, _, notify = build(sessions, busy=True)
        await linked(services)
        stores = services.habits
        assert stores is not None
        stores.receipts_in_flight.add(USER)
        from tests.fakes import CONTEXT

        await refresh_receipts(services, USER, CONTEXT)
        assert await stores.imports.last_ok(USER, "offline") is None
        assert USER not in stores.receipts_in_flight
        assert notify.sent == []


class TestRendering:
    def test_the_usual_list_and_a_nudge_open_the_mini_app_on_the_habits_screen(self) -> None:
        from komora.bot.render import to_reply as render

        (habit,) = compute_habits(
            offline_purchases({"orders": weekly_receipts()}), since=date(2026, 6, 1)
        )[:1]
        today = date(2026, 7, 27)
        url = "https://t.me/bot/app"
        listed = render(HabitsReady(habits=[habit], today=today), url)
        assert f"{url}?startapp=usual" in [b.url for b in listed.buttons]
        nudge = render(NudgeReady(habits=[habit], today=today), url)
        assert f"{url}?startapp=usual" in [b.url for b in nudge.buttons]
        # Nothing tracked: the screen would say the same sentence, so no door to it.
        empty = render(HabitsReady(habits=[], today=today), url)
        assert empty.buttons == ()
        assert render(HabitsReady(habits=[habit], today=today)).buttons[-1].url is None
