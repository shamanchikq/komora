"""The Sunday digest (Plan 4 Task 4): one message from stored data, opt-in.

Every number is read from what Silpo actually charged — receipt totals and their
`sumDiscount`, delivered orders' lines — never inferred from a coupon. Two sources are
shown as two lines until it is known whether a delivery also appears as a receipt
(Plan 3's open question); adding them before that could count a purchase twice.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from komora.core.habits.engine import Habit
from komora.core.habits.purchases import ReceiptTotals
from komora.core.money import CURRENCY, uah
from komora.core.text import days


@dataclass(frozen=True)
class ExpiringCoupon:
    text: str
    ends_on: date


@dataclass(frozen=True)
class DigestInput:
    week_start: date
    week_end: date
    """Exclusive."""
    online_spent: Decimal
    online_lines: int
    receipts: list[ReceiptTotals]
    budget_cap: int | None
    due_next_week: list[Habit]
    expiring: list[ExpiringCoupon] = field(default_factory=list)


DIGEST_TITLE = "Підсумок тижня"
MAX_DUE_NAMED = 5


def digest_text(digest: DigestInput, today: date) -> str | None:
    """The message, or `None` when there is nothing true to say.

    An empty digest is not sent: «витрачено 0 ₴, заощаджено 0 ₴» about a week Komora
    simply did not see would be a claim about the fridge.
    """
    receipt_total = sum((r.total for r in digest.receipts), Decimal("0"))
    receipt_discount = sum((r.discount for r in digest.receipts), Decimal("0"))
    bonuses = sum((r.bonuses_accrued for r in digest.receipts), Decimal("0"))
    saw_spend = bool(digest.receipts) or digest.online_lines > 0
    if not saw_spend and not digest.due_next_week and not digest.expiring:
        return None

    blocks = [f"<b>{DIGEST_TITLE}</b> · {digest.week_start:%d.%m}–{_last_day(digest):%d.%m}", ""]

    if saw_spend:
        blocks.append("<b>Витрачено</b>")
        if digest.receipts:
            n = len(digest.receipts)
            blocks.append(f"• у магазині: {uah(receipt_total)} за {n} {_receipts(n)}")
        if digest.online_lines > 0:
            blocks.append(f"• онлайн: {uah(digest.online_spent)} за замовленнями")
        if digest.receipts and digest.online_lines > 0:
            blocks.append("Дві суми окремо: чи є доставка також у чеках, Комора ще не знає.")
        if digest.budget_cap is not None:
            spent = receipt_total + digest.online_spent
            left = Decimal(digest.budget_cap) - spent
            blocks.append(
                f"Бюджет {digest.budget_cap} {CURRENCY} — лишилося {uah(left)}"
                if left >= 0
                else f"Бюджет {digest.budget_cap} {CURRENCY} — перевищено на {uah(-left)}"
            )
        if receipt_discount > 0 or bonuses > 0:
            blocks.append("")
            blocks.append("<b>Заощаджено</b> — за даними чеків")
            if receipt_discount > 0:
                blocks.append(f"• знижки в чеках: {uah(receipt_discount)}")
            if bonuses > 0:
                blocks.append(f"• нараховано балабонусів: {bonuses:.0f}")

    if digest.due_next_week:
        blocks += ["", "<b>Наступного тижня, схоже, знадобиться</b>"]
        for habit in digest.due_next_week[:MAX_DUE_NAMED]:
            blocks.append(f"• {habit.name} — кожні ~{days(round(habit.median_gap_days))}")
        rest = len(digest.due_next_week) - MAX_DUE_NAMED
        if rest > 0:
            blocks.append(f"…і ще {rest}")

    if digest.expiring:
        blocks += ["", "<b>Купон згорає</b>"]
        for coupon in digest.expiring:
            blocks.append(f"• {coupon.text} — до {coupon.ends_on:%d.%m}")

    blocks += ["", "«/digest off» — більше не надсилати."]
    return "\n".join(blocks)


def _last_day(digest: DigestInput) -> date:
    from datetime import timedelta

    return digest.week_end - timedelta(days=1)


def _receipts(n: int) -> str:
    from komora.core.text import pl

    return pl(n, "чек", "чеки", "чеків")
