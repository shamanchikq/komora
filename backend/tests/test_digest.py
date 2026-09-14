"""The Sunday digest (Plan 4 Task 4): stored data only, opt-in, once a week."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker

from komora.bot.habits_job import tick
from komora.bot.handlers import (
    DIGEST_KIND,
    DIGEST_OFF,
    DIGEST_ON,
    digest_for,
    on_digest,
    refresh_history,
    send_digest_if_due,
)
from komora.bot.outcomes import Spoke
from komora.core.digest import DigestInput, ExpiringCoupon, digest_text
from komora.core.habits.purchases import ReceiptTotals, receipt_totals
from komora.db.repo import ReceiptRepo, UserRepo
from tests.fakes import FakeSilpo, product
from tests.test_deals import habit
from tests.test_habits_handlers import MILK, USER, build, linked, receipt

SUNDAY_EVENING = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)  # 19:00 Kyiv, a Sunday
WEEK_START = date(2026, 9, 14)


def totals(day: date, total: str, discount: str = "0", bonuses: str = "0") -> ReceiptTotals:
    return ReceiptTotals(
        receipt_key=f"r:{day}",
        bought_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=10),
        total=Decimal(total),
        discount=Decimal(discount),
        bonuses_accrued=Decimal(bonuses),
    )


def digest_input(**overrides: object) -> DigestInput:
    base = dict(
        week_start=WEEK_START,
        week_end=WEEK_START + timedelta(days=7),
        online_spent=Decimal("0"),
        online_lines=0,
        receipts=[],
        budget_cap=None,
        due_next_week=[],
        expiring=[],
    )
    return DigestInput(**{**base, **overrides})  # type: ignore[arg-type]


class TestReceiptTotals:
    def test_sum_reg_is_trusted_only_when_it_matches_the_lines(self) -> None:
        payload = {
            "orders": [
                {
                    **receipt(1, WEEK_START, (MILK, "Молоко", 1)),
                    "sumReg": 40.4,
                    "sumDiscount": 5,
                    "accruedBalaBonusesSum": 1.5,
                },
                {**receipt(2, WEEK_START, (MILK, "Молоко", 1)), "sumReg": 400},
            ]
        }
        first, second = receipt_totals(payload)
        assert first.total == Decimal("40.40") and first.discount == Decimal("5.00")
        assert first.bonuses_accrued == Decimal("1.50")
        assert second.total == Decimal("40.00"), "a total two fields disagree on is the line sum"

    async def test_stored_by_the_importer_and_read_back_by_week(
        self, sessions: async_sessionmaker
    ) -> None:
        day = WEEK_START + timedelta(days=2)
        silpo = FakeSilpo(
            {},
            offline_orders=[
                {**receipt(1, day, (MILK, "Молоко", 1)), "sumReg": 40, "sumDiscount": 3}
            ],
        )
        services, _, _ = build(sessions, silpo=silpo)
        await linked(services, USER)
        async with services.connect(USER) as mcp:  # type: ignore[attr-defined]
            await refresh_history(services, USER, mcp, now=SUNDAY_EVENING)
        repo = ReceiptRepo(sessions)
        start = datetime(2026, 9, 13, 21, tzinfo=UTC)
        [row] = await repo.between(USER, start, start + timedelta(days=7))
        assert row.total == Decimal("40.00") and row.discount == Decimal("3.00")
        assert await repo.between(USER, start + timedelta(days=7), start + timedelta(days=14)) == []


class TestDigestText:
    def test_nothing_true_to_say_is_none(self) -> None:
        assert digest_text(digest_input(), WEEK_START) is None

    def test_receipts_only(self) -> None:
        text = digest_text(
            digest_input(
                receipts=[
                    totals(WEEK_START, "120.50", "20", "3"),
                    totals(WEEK_START + timedelta(days=1), "79.50"),
                ]
            ),
            WEEK_START,
        )
        assert text is not None
        assert "у магазині: 200,00 ₴ за 2 чеки" in text
        assert "знижки в чеках: 20,00 ₴" in text and "балабонусів: 3" in text
        assert "онлайн" not in text and "Дві суми окремо" not in text

    def test_both_sources_stay_two_numbers(self) -> None:
        text = digest_text(
            digest_input(
                receipts=[totals(WEEK_START, "100")], online_spent=Decimal("50"), online_lines=2
            ),
            WEEK_START,
        )
        assert text is not None
        assert "у магазині: 100,00 ₴" in text and "онлайн: 50,00 ₴" in text
        assert "Дві суми окремо" in text and "150" not in text

    def test_budget_over_and_under(self) -> None:
        under = digest_text(
            digest_input(receipts=[totals(WEEK_START, "100")], budget_cap=500), WEEK_START
        )
        over = digest_text(
            digest_input(receipts=[totals(WEEK_START, "600")], budget_cap=500), WEEK_START
        )
        assert under and "лишилося 400,00 ₴" in under
        assert over and "перевищено на 100,00 ₴" in over

    def test_habits_and_coupons_without_any_spend(self) -> None:
        text = digest_text(
            digest_input(
                due_next_week=[habit()],
                expiring=[
                    ExpiringCoupon(
                        text="−10% на онлайн чек", ends_on=WEEK_START + timedelta(days=6)
                    )
                ],
            ),
            WEEK_START,
        )
        assert text is not None
        assert "Молоко Галичина — кожні ~7 днів" in text
        assert "−10% на онлайн чек — до 20.09" in text
        assert "Витрачено" not in text


class TestDigestHandlers:
    async def test_the_command_switches_and_explains(self, sessions: async_sessionmaker) -> None:
        services, _, _ = build(sessions)
        assert isinstance(await on_digest(services, USER, ""), Spoke)
        on = await on_digest(services, USER, "on")
        assert isinstance(on, Spoke) and on.text == DIGEST_ON
        user = await UserRepo(sessions).get(USER)
        assert user is not None and user.digest_weekly
        off = await on_digest(services, USER, "off")
        assert isinstance(off, Spoke) and off.text == DIGEST_OFF

    async def test_digest_for_reads_the_week_from_stored_rows(
        self, sessions: async_sessionmaker
    ) -> None:
        services, _, _ = build(sessions)
        await linked(services, USER)
        await ReceiptRepo(sessions).upsert(
            USER, [totals(WEEK_START + timedelta(days=3), "150", "12")]
        )
        stores = services.habits
        assert stores is not None
        await stores.habits.replace(USER, [habit()])
        spoke = await digest_for(services, USER, now=SUNDAY_EVENING)
        assert spoke is not None
        assert "150,00 ₴ за 1 чек" in spoke.text and "знижки в чеках: 12,00 ₴" in spoke.text
        assert "Молоко Галичина" in spoke.text

    async def test_expiring_coupons_only_with_a_session(self, sessions: async_sessionmaker) -> None:
        silpo = FakeSilpo(
            {},
            coupons=[
                {
                    "id": 1,
                    "active": True,
                    "rewardText": "−10%",
                    "description": "чек",
                    "endDate": "2026-09-23",
                }
            ],
        )
        services, _, _ = build(sessions, silpo=silpo)
        await linked(services, USER)
        await ReceiptRepo(sessions).upsert(USER, [totals(WEEK_START, "10")])
        without = await digest_for(services, USER, now=SUNDAY_EVENING)
        with_session = await digest_for(services, USER, now=SUNDAY_EVENING, mcp=silpo)
        assert without and "згорає" not in without.text
        assert with_session and "−10% чек — до 23.09" in with_session.text

    async def test_sent_once_on_sunday_evening_to_subscribers_only(
        self, sessions: async_sessionmaker
    ) -> None:
        services, _, notify = build(sessions)
        await linked(services, USER)
        await ReceiptRepo(sessions).upsert(USER, [totals(WEEK_START, "10")])
        assert not await send_digest_if_due(services, USER, now=SUNDAY_EVENING), "not subscribed"
        await services.users.set_digest(USER, True)
        assert not await send_digest_if_due(services, USER, now=SUNDAY_EVENING - timedelta(days=1))
        assert await send_digest_if_due(services, USER, now=SUNDAY_EVENING)
        assert not await send_digest_if_due(services, USER, now=SUNDAY_EVENING + timedelta(hours=1))
        stores = services.habits
        assert stores is not None
        assert await stores.notifications.last_sent(USER, DIGEST_KIND, "2026-W38") == SUNDAY_EVENING
        assert len(notify.sent) == 1

    async def test_the_job_tick_carries_it(self, sessions: async_sessionmaker) -> None:
        silpo = FakeSilpo({"815253": [product("Молоко Галичина", 42.9, product_id=MILK)]})
        services, _, notify = build(sessions, silpo=silpo)
        await linked(services, USER)
        await services.users.set_digest(USER, True)
        await ReceiptRepo(sessions).upsert(USER, [totals(WEEK_START, "10")])
        await tick(services, now=SUNDAY_EVENING)
        assert any(isinstance(o, Spoke) and "Підсумок тижня" in o.text for _, o in notify.sent)
