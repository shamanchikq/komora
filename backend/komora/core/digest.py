"""The Sunday digest (Plan 4 Task 4): one message from stored data, opt-in.

Every number is read from what Silpo actually charged — receipt totals and their
`sumDiscount`, delivered orders' lines — never inferred from a coupon. Two sources are
shown as two lines until it is known whether a delivery also appears as a receipt
(Plan 3's open question); adding them before that could count a purchase twice.

This module holds the figures and the rules about what they support; the wording is
`bot/render.render_digest`, like every other message.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from komora.core.habits.engine import Habit
from komora.core.habits.purchases import ReceiptTotals


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


def has_news(digest: DigestInput) -> bool:
    """Whether there is anything true to say. An empty digest is not sent: «витрачено
    0 ₴, заощаджено 0 ₴» about a week Komora simply did not see would be a claim about
    the fridge."""
    saw_spend = bool(digest.receipts) or digest.online_lines > 0
    return saw_spend or bool(digest.due_next_week) or bool(digest.expiring)


@dataclass(frozen=True)
class BudgetStanding:
    """The week's spend against the weekly cap, as far as the data can say it.

    `exact` is false when both sources were seen: whether a delivery also appears as a
    receipt is unknown, so the true spend lies between the larger of the two and their
    sum. Only what holds at both ends is claimed — «перевищено щонайменше на …» when
    even the lower figure is over, «лишилося щонайменше …» when even the sum is
    under, and nothing in between. Adding the two, which the budget line did, was the
    one combined number this digest says it never prints.
    """

    over: Decimal | None
    left: Decimal | None
    exact: bool


def budget_standing(digest: DigestInput) -> BudgetStanding | None:
    if digest.budget_cap is None:
        return None
    cap = Decimal(digest.budget_cap)
    receipts = sum((r.total for r in digest.receipts), Decimal("0"))
    online = digest.online_spent if digest.online_lines > 0 else Decimal("0")
    both = bool(digest.receipts) and digest.online_lines > 0
    low = max(receipts, online) if both else receipts + online
    high = receipts + online
    if low > cap:
        return BudgetStanding(over=low - cap, left=None, exact=not both)
    if high <= cap:
        return BudgetStanding(over=None, left=cap - high, exact=not both)
    return BudgetStanding(over=None, left=None, exact=False)
