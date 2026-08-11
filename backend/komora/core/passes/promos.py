"""Savings pass.

**Scope correction, from the captured schemas.** The spec assumed coupons expose which
products trigger them, so a cart could be optimised toward them. They do not:
`silpo_get_my_coupons` and `silpo_get_coupon_details` return `rewardValue` plus prose
(`description`, `limitText`, `warningText`) and **no eligible-product list**. Likewise
`silpo_get_promotions` returns only a code, title, product count and URL - no amounts.

What Silpo does expose is machine-readable and better: every product carries `price`
and `oldPrice`, with the discount already applied. `oldPrice - price` is therefore a
real, current saving that needs no inference.

So this pass reports savings that genuinely exist, and surfaces the user's coupons as
information rather than pretending to apply them. Silpo applies coupons at the till,
which is also what its own tool descriptions say.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from komora.core.models import ResolvedCart
from komora.core.money import uah

DEGRADED_COUPONS = "degraded:coupons"


def apply_savings(cart: ResolvedCart) -> ResolvedCart:
    """Total the discounts already present in the resolved prices."""
    saved = Decimal("0")
    notes: list[str] = []

    for line in cart.lines:
        if line.unavailable or line.old_price is None or line.old_price <= line.unit_price:
            continue
        amount = (line.old_price - line.unit_price) * Decimal(str(line.qty))
        saved += amount
        # Not f"{amount} ₴": Decimal keeps its operands' scale, so this reached a live
        # run as «знижка 15.000 ₴».
        notes.append(f"{line.name} — знижка {uah(amount)}")

    return cart.model_copy(
        update={"estimated_savings": saved, "savings_notes": [*cart.savings_notes, *notes]}
    )


def describe_coupons(coupons: Sequence[dict[str, Any]]) -> list[str]:
    """Turn the user's coupons into readable notes.

    Deliberately not matched against the cart: Silpo publishes no mapping from coupon
    to product, so claiming a given coupon applies to a given line would be invention.

    `limitText` is deliberately **not** included. A real coupon's looks like this:

        • Не діє на подарункові сертифікати, тютюнові вироби та стартові пакети
        • Пропозиція не діє на доставку LOKO.
        • Діє лише при замовленні доставки на silpo.ua або в застосунку.

    — three lines of bullets, which read as broken output inside a one-line note. The
    conditions live in the Silpo app, where they are formatted for reading.

    `rewardText` only exists if the caller enriched the coupon from
    `get_coupon_details`; `get_my_coupons` cannot return it (its schema is
    `additionalProperties: false`). Without it a coupon's own `description` can be a
    fragment — "на онлайн чек" — so `warningText` is appended when present, since that
    is where the cap lives ("Максимальна знижка 100 грн").
    """
    notes: list[str] = []
    for coupon in coupons:
        if not coupon.get("active"):
            continue

        reward = _clean(coupon.get("rewardText"))
        description = _clean(coupon.get("description"))
        headline = " ".join(part for part in (reward, description) if part)
        if not headline:
            continue

        warning = _clean(coupon.get("warningText"))
        notes.append(f"{headline} — {warning}" if warning else headline)
    return notes


def _clean(value: Any) -> str:
    """One line, collapsed whitespace. Silpo's coupon prose carries CRLF."""
    return " ".join(str(value or "").split())
