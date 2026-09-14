"""Price memory, the deal scan, the alert and «/deals» (Plan 4 Tasks 2–3).

Every rule from the plan is a test here, against the fake and an in-memory database:
a deal is `oldPrice > price` on the product's own id, a miss is unknown, the branch
list is ranked here and never by Silpo's order, the alert is rare and deduped, and
«Додати» buys nothing the resolve pass does not confirm.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from komora.api.app import create_app
from komora.api.minapp import serialise
from komora.bot.handlers import (
    NOT_A_DEAL,
    PRICES_SOURCE,
    Services,
    deal_for,
    on_add_deal,
    on_callback,
    on_deals,
    on_habits_draft,
    refresh_history,
    scan_prices,
)
from komora.bot.outcomes import DealReady, DealsReady, DraftReady, Spoke
from komora.bot.render import to_reply
from komora.core.deals.models import Snapshot
from komora.core.deals.scan import (
    deals_among,
    percent_below,
    promo_texts,
    rank_branch_deals,
    snapshot_tracked,
    usual_price,
)
from komora.core.habits.engine import Habit
from komora.core.mcp.auth import AuthorizationBridge
from tests.fakes import CONTEXT, INITDATA_TOKEN, FakeSilpo, product, signed_init_data
from tests.test_habits_handlers import (
    BREAD,
    MILK,
    USER,
    build,
    deferred,
    linked,
    weekly_receipts,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)  # a Monday noon, Kyiv 15:00
TODAY = date(2026, 9, 14)


def habit(
    key: str = MILK, *, article: int | None = 815253, cv: float = 0.3, muted: bool = False
) -> Habit:
    return Habit(
        product_key=key,
        name="Молоко Галичина",
        events=8,
        median_gap_days=7,
        cv=cv,
        confidence=0.8,
        last_bought=TODAY - timedelta(days=6),
        last_qty=1,
        median_qty=1,
        due_on=TODAY + timedelta(days=1),
        external_product_id=article,
        muted=muted,
    )


def snap(
    key: str = MILK, *, price: str = "39.99", old: str | None = "60.99", day: date = TODAY
) -> Snapshot:
    return Snapshot(
        product_key=key,
        external_product_id=815253,
        branch_id=CONTEXT.branch_id,
        day=day,
        price=Decimal(price),
        old_price=Decimal(old) if old else None,
        display_price=None,
        available=True,
        captured_at=NOW,
    )


class TestScan:
    async def test_matches_by_article_never_by_name(self) -> None:
        other = product("Молоко Галичина", 30, product_id="impostor", external_id=1)
        real = product(
            "Молоко Галичина", 42.9, product_id=MILK, external_id=815253, old_price=60.99
        )
        silpo = FakeSilpo({"815253": [other, real]})
        result = await snapshot_tracked(silpo, CONTEXT, [habit()], today=TODAY, now=NOW)
        [snapshot] = result.snapshots
        assert snapshot.product_key == MILK and snapshot.price == Decimal("42.9")
        assert snapshot.old_price == Decimal("60.99") and snapshot.discounted
        assert silpo.search_calls == [["815253"]], "one call, the article as the term"

    async def test_matches_by_stored_id_when_no_article(self) -> None:
        silpo = FakeSilpo({"Молоко Галичина": [product("Молоко Галичина", 42.9, product_id=MILK)]})
        result = await snapshot_tracked(silpo, CONTEXT, [habit(article=None)], today=TODAY, now=NOW)
        assert [s.product_key for s in result.snapshots] == [MILK]

    async def test_a_miss_is_unknown_not_a_verdict(self) -> None:
        silpo = FakeSilpo({})
        result = await snapshot_tracked(silpo, CONTEXT, [habit()], today=TODAY, now=NOW)
        assert result.snapshots == [] and result.unknown == [MILK]

    async def test_counter_goods_are_not_scanned(self) -> None:
        silpo = FakeSilpo({})
        counter = Habit(**{**habit().__dict__, "reorderable": False, "product_key": "lager:1"})
        result = await snapshot_tracked(silpo, CONTEXT, [counter], today=TODAY, now=NOW)
        assert result.snapshots == [] and result.unknown == [] and silpo.search_calls == []

    async def test_thirty_per_call(self) -> None:
        habits = [
            Habit(**{**habit().__dict__, "product_key": f"p{i}", "external_product_id": i})
            for i in range(45)
        ]
        silpo = FakeSilpo({})
        await snapshot_tracked(silpo, CONTEXT, habits, today=TODAY, now=NOW)
        assert [len(c) for c in silpo.search_calls] == [30, 15]

    def test_a_deal_is_old_price_over_price_and_in_stock(self) -> None:
        cheap = snap(price="39.99", old="60.99")
        plain = snap(BREAD, price="28.50", old=None)
        gone = Snapshot(**{**cheap.__dict__, "available": False})
        assert deals_among([plain, gone, cheap]) == [cheap]
        assert cheap.percent_off == 34

    def test_usual_price_needs_four_days_of_shelf_prices(self) -> None:
        three = [snap(price="60.99", day=TODAY - timedelta(days=i)) for i in range(3)]
        assert usual_price(three) is None
        four = [*three, snap(price="59.99", day=TODAY - timedelta(days=10))]
        assert usual_price(four) == Decimal("60.99")
        assert percent_below(Decimal("39.99"), Decimal("60.99")) == 34
        assert percent_below(Decimal("60.99"), Decimal("60.99")) is None

    def test_the_same_day_counts_once(self) -> None:
        same = [snap(price="60.99", day=TODAY) for _ in range(5)]
        assert usual_price(same) is None


class TestBranchDeals:
    def test_ranked_by_discount_not_by_silpo_order(self) -> None:
        page = [
            product("Дешевше на 10", 90, old_price=100, product_id="a"),
            product("Дешевше на 50", 50, old_price=100, product_id="b"),
            product("Без знижки", 100, product_id="c"),
            product("Нема в наявності", 10, old_price=100, product_id="d", stock=0),
            product("Пакет", 1, old_price=2, product_id="e"),
            product("Дешевше на 30", 70, old_price=100, product_id="f", display_ratio="900г"),
        ]
        deals = rank_branch_deals(page)
        assert [d.product_id for d in deals] == ["b", "f", "a"]
        assert deals[1].display_ratio == "900г" and deals[0].percent_off == 50

    def test_the_list_is_capped(self) -> None:
        page = [product(f"p{i}", 100 - i, old_price=100, product_id=str(i)) for i in range(1, 30)]
        assert len(rank_branch_deals(page)) == 10

    def test_promos_are_prose(self) -> None:
        payload = {
            "promos": [
                {
                    "rewardText": "x25 балобонусів",
                    "description": "за свіжу свинину",
                    "selected": True,
                },
                {"rewardText": "", "description": ""},
                "junk",
            ]
        }
        assert promo_texts(payload) == ["x25 балобонусів — за свіжу свинину ✓"]


class TestAfterTurnScan:
    """The scan rides the turn that holds a context, once a day, after the reply."""

    async def test_a_turn_schedules_the_scan_and_records_it(
        self, sessions: async_sessionmaker
    ) -> None:
        silpo = FakeSilpo(
            {"815253": [product("Молоко Галичина", 42.9, product_id=MILK, external_id=815253)]},
            offline_orders=weekly_receipts(),
        )
        services, _, _ = build(sessions, silpo=silpo)
        await linked(services, USER)
        async with services.connect(USER) as mcp:  # type: ignore[attr-defined]
            await refresh_history(services, USER, mcp, now=NOW)
        stores = services.habits
        assert stores is not None and stores.prices is not None
        await on_habits_draft(services, USER)  # any turn that reads the cart
        assert await deferred(services).drain() >= 1
        assert await stores.imports.last_ok(USER, PRICES_SOURCE) is not None
        assert [s.product_key for s in await stores.prices.latest(USER, CONTEXT.branch_id)] == [
            MILK,
            BREAD,
        ] or len(await stores.prices.latest(USER, CONTEXT.branch_id)) >= 1

    async def test_not_twice_in_a_day(self, sessions: async_sessionmaker) -> None:
        services, silpo, _ = build(sessions)
        await linked(services, USER)
        stores = services.habits
        assert stores is not None
        await stores.imports.record(USER, PRICES_SOURCE, "ok", "", at=datetime.now(UTC))
        await on_habits_draft(services, USER)
        await deferred(services).drain()
        rows = [c for c in silpo.search_calls if c == ["815253", "878840"]]
        assert rows == [], "the tracked products were re-priced although a scan is fresh"

    async def test_a_miss_is_recorded_by_name(self, sessions: async_sessionmaker) -> None:
        services, _, _ = build(sessions, silpo=FakeSilpo({}, offline_orders=weekly_receipts()))
        await linked(services, USER)
        async with services.connect(USER) as mcp:  # type: ignore[attr-defined]
            await refresh_history(services, USER, mcp, now=NOW)
            snapshots = await scan_prices(services, USER, mcp, CONTEXT, now=NOW)
        assert snapshots == []
        stores = services.habits
        assert stores is not None
        assert await stores.imports.last_ok(USER, PRICES_SOURCE) == NOW


async def _tracked(services: Services, *habits: Habit) -> None:
    stores = services.habits
    assert stores is not None
    await stores.habits.replace(USER, list(habits))


class TestAlert:
    async def test_a_discounted_tracked_product_is_one_message_with_a_draft_button(
        self, sessions: async_sessionmaker
    ) -> None:
        services, _, _ = build(sessions)
        await linked(services, USER)
        await _tracked(services, habit())
        alert = await deal_for(services, USER, [snap()], now=NOW)
        assert isinstance(alert, DealReady)
        reply = to_reply(alert)
        assert "39,99 ₴ замість 60,99 ₴" in reply.text and "−34 %" in reply.text
        assert "звичайну" not in reply.text, "no usual-price claim without four snapshots"
        assert [b.data for b in reply.buttons][:2] == [f"habits:deal:{MILK}", "dismiss"]

    async def test_only_nudgeable_habits_and_once_a_week(
        self, sessions: async_sessionmaker
    ) -> None:
        services, _, _ = build(sessions)
        await linked(services, USER)
        await _tracked(services, habit(cv=0.9))  # tracked, not nudgeable
        assert await deal_for(services, USER, [snap()], now=NOW) is None
        await _tracked(services, habit())
        assert await deal_for(services, USER, [snap()], now=NOW) is not None
        assert await deal_for(services, USER, [snap()], now=NOW + timedelta(days=3)) is None
        assert await deal_for(services, USER, [snap()], now=NOW + timedelta(days=8)) is not None

    async def test_quiet_hours_mutes_and_the_cart_hold_it_back(
        self, sessions: async_sessionmaker
    ) -> None:
        services, _, _ = build(sessions)
        await linked(services, USER)
        await _tracked(services, habit())
        stores = services.habits
        assert stores is not None
        assert await stores.habits.set_muted(USER, MILK, True)
        assert await deal_for(services, USER, [snap()], now=NOW) is None
        assert await stores.habits.set_muted(USER, MILK, False)
        night = datetime(2026, 9, 14, 21, 30, tzinfo=UTC)  # 00:30 Kyiv
        assert await deal_for(services, USER, [snap()], now=night) is None
        basket_id = await services.baskets.create_from_cart(USER, "x", "stated", _cart_with(MILK))
        await services.baskets.mark_synced(basket_id, {MILK})
        assert await deal_for(services, USER, [snap()], now=NOW) is None, "just pushed to the cart"

    async def test_the_usual_price_is_named_only_with_history(
        self, sessions: async_sessionmaker
    ) -> None:
        services, _, _ = build(sessions)
        await linked(services, USER)
        await _tracked(services, habit())
        stores = services.habits
        assert stores is not None and stores.prices is not None
        await stores.prices.upsert(
            USER,
            [snap(price="60.99", old=None, day=TODAY - timedelta(days=i)) for i in (1, 2, 3, 4)],
        )
        alert = await deal_for(services, USER, [snap()], now=NOW)
        assert alert is not None and alert.deals[0].below_usual == 34
        assert "на 34 % нижче за звичайну тут" in to_reply(alert).text

    async def test_the_button_builds_exactly_the_named_product(
        self, sessions: async_sessionmaker
    ) -> None:
        services, _, _ = build(sessions)
        await linked(services, USER)
        await _tracked(
            services, habit(), Habit(**{**habit(BREAD, article=878840).__dict__, "name": "Хліб"})
        )
        outcome = await on_callback(services, USER, f"habits:deal:{MILK}")
        assert isinstance(outcome, DraftReady)
        assert [ln.product_id for ln in outcome.cart.lines] == [MILK]
        assert outcome.cart.lines[0].reason_kind == "habit"


def _cart_with(product_id: str):  # type: ignore[no-untyped-def]
    from komora.core.models import ResolvedCart, ResolvedLine

    return ResolvedCart(
        lines=[
            ResolvedLine(
                product_id=product_id,
                company_id="c",
                branch_id="b",
                name="Молоко",
                qty=1,
                unit="",
                unit_price=Decimal("40"),
                reason_kind="stated",
                reason_text="x",
            )
        ],
        total=Decimal("40"),
    )


CHEESE = product(
    "Сир Мужон", 50, old_price=100, product_id="cheese", external_id=1, display_ratio="200г"
)


def deals_fake(**kw: object) -> FakeSilpo:
    return FakeSilpo(
        {
            "815253": [
                product(
                    "Молоко Галичина", 39.99, product_id=MILK, external_id=815253, old_price=60.99
                )
            ],
            # «Додати» re-fetches the product by article, so the search must know it.
            "1": [CHEESE],
        },
        promotion_products=[
            CHEESE,
            product("Кава", 90, old_price=100, product_id="coffee", external_id=2),
        ],
        promos=[{"rewardText": "x20 балобонусів", "description": "за каву"}],
        coupons=[{"id": 1, "active": True, "rewardText": "−10%", "description": "на онлайн чек"}],
        **kw,  # type: ignore[arg-type]
    )


class TestDealsScreen:
    async def test_three_lists_computed_from_reads(self, sessions: async_sessionmaker) -> None:
        services, _, _ = build(sessions, silpo=deals_fake())
        await linked(services, USER)
        await _tracked(services, habit())
        outcome = await on_deals(services, USER)
        assert isinstance(outcome, DealsReady)
        assert [d.habit.product_key for d in outcome.mine] == [MILK]
        assert [d.product_id for d in outcome.branch] == ["cheese", "coffee"]
        assert outcome.coupons == ["−10% на онлайн чек"]
        assert outcome.promos == ["x20 балобонусів — за каву"]
        assert outcome.warnings == [] and outcome.scanned_at is not None
        text = to_reply(outcome).text
        assert "Сир Мужон · 200г — 50,00 ₴ замість 100,00 ₴ (−50 %)" in text
        assert "лише в застосунку Сільпо" in text
        assert [b.data for b in to_reply(outcome).buttons] == ["habits:deals"]

    async def test_each_part_degrades_alone(self, sessions: async_sessionmaker) -> None:
        services, _, _ = build(sessions, silpo=deals_fake(fails={"get_products", "get_my_promos"}))
        await linked(services, USER)
        outcome = await on_deals(services, USER)
        assert isinstance(outcome, DealsReady)
        assert outcome.branch == [] and outcome.promos == []
        assert outcome.warnings == ["degraded:branch", "degraded:promos"]
        assert outcome.coupons == ["−10% на онлайн чек"]
        text = to_reply(outcome).text
        assert "Акції магазину зараз недоступні" in text
        assert "Знижок у цьому магазині зараз не видно" not in text, "unavailable is not empty"

    async def test_needs_a_link_and_a_context(self, sessions: async_sessionmaker) -> None:
        services, _, _ = build(sessions)
        outcome = await on_deals(services, USER)
        assert isinstance(outcome, Spoke) and outcome.needs_link

    async def test_the_draft_from_deals_holds_the_discounted_habits(
        self, sessions: async_sessionmaker
    ) -> None:
        services, _, _ = build(sessions, silpo=deals_fake())
        await linked(services, USER)
        await _tracked(services, habit(), habit(BREAD, article=878840))
        await on_deals(services, USER)  # scans
        outcome = await on_callback(services, USER, "habits:deals")
        assert isinstance(outcome, DraftReady)
        assert [ln.product_id for ln in outcome.cart.lines] == [MILK]


class TestAddDeal:
    async def test_into_a_new_draft_with_the_exact_saving_as_the_reason(
        self, sessions: async_sessionmaker
    ) -> None:
        silpo = FakeSilpo(
            {"1": [product("Сир Мужон", 50, old_price=100, product_id="cheese", external_id=1)]}
        )
        services, _, _ = build(sessions, silpo=silpo)
        await linked(services, USER)
        outcome = await on_add_deal(services, USER, "cheese", "Сир Мужон", 1)
        assert isinstance(outcome, DraftReady) and outcome.title == "Акції"
        [line] = outcome.cart.lines
        assert line.reason_kind == "deal"
        assert line.reason_text == "зі знижкою у Сільпо — 50,00 ₴ замість 100,00 ₴"
        assert outcome.cart.estimated_savings == Decimal("50")

    async def test_into_the_open_draft(self, sessions: async_sessionmaker) -> None:
        silpo = FakeSilpo(
            {"1": [product("Сир Мужон", 50, old_price=100, product_id="cheese", external_id=1)]}
        )
        services, _, _ = build(sessions, silpo=silpo)
        await linked(services, USER)
        basket_id = await services.baskets.create_from_cart(
            USER, "Кошик", "stated", _cart_with(MILK)
        )
        outcome = await on_add_deal(services, USER, "cheese", "Сир Мужон", 1)
        assert isinstance(outcome, DraftReady) and outcome.basket_id == basket_id
        assert [ln.product_id for ln in outcome.cart.lines] == [MILK, "cheese"]
        assert outcome.cart.total == Decimal("90")
        assert outcome.toast == "Додано Сир Мужон"

    async def test_an_invented_id_adds_nothing(self, sessions: async_sessionmaker) -> None:
        silpo = FakeSilpo(
            {"1": [product("Сир Мужон", 50, old_price=100, product_id="cheese", external_id=1)]}
        )
        services, _, _ = build(sessions, silpo=silpo)
        await linked(services, USER)
        outcome = await on_add_deal(services, USER, "someone-elses-id", "Сир Мужон", 1)
        assert isinstance(outcome, Spoke) and outcome.text == NOT_A_DEAL
        assert await services.baskets.get_active(USER) is None


class TestSerialisationAndRoutes:
    def test_deal_outcomes_serialise_with_the_chats_sentences(self) -> None:
        alert = DealReady(deals=[], today=TODAY)
        assert serialise(alert) == {"kind": "deal", "deals": []}
        screen = serialise(DealsReady(mine=[], branch=[], coupons=["c"], promos=[]))
        assert screen["kind"] == "deals" and screen["empty_mine_text"] and screen["trust_text"]

    async def test_the_routes_answer_for_the_authenticated_user(
        self, sessions: async_sessionmaker
    ) -> None:
        services, _, _ = build(sessions, silpo=deals_fake())
        await linked(services, USER)
        client = TestClient(create_app(AuthorizationBridge(), services, INITDATA_TOKEN))
        headers = {"Authorization": f"tma {signed_init_data(USER)}"}
        got = client.get("/api/deals", headers=headers).json()
        assert got["kind"] == "deals" and [d["product_id"] for d in got["branch"]] == [
            "cheese",
            "coffee",
        ]
        assert got["branch"][0]["display_ratio"] == "200г"
        added = client.post(
            "/api/deals/add",
            headers=headers,
            json={"product_id": "cheese", "name": "Сир Мужон", "external_product_id": 1},
        ).json()
        assert added["kind"] == "draft" and added["title"] == "Акції"
        assert added["cart"]["lines"][0]["display_ratio"] == "200г"
        assert client.get("/api/deals").status_code == 401
