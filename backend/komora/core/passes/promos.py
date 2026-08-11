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
        notes.append(f"{line.name} — знижка {amount} ₴")

    return cart.model_copy(
        update={"estimated_savings": saved, "savings_notes": [*cart.savings_notes, *notes]}
    )


def describe_coupons(coupons: Sequence[dict[str, Any]]) -> list[str]:
    """Turn the user's coupons into readable notes.

    Deliberately not matched against the cart: Silpo publishes no mapping from coupon
    to product, so claiming a given coupon applies to a given line would be invention.
    The conditions live in `limitText` as Ukrainian prose, which the user can read and
    we cannot reliably parse.
    """
    notes: list[str] = []
    for coupon in coupons:
        if not coupon.get("active"):
            continue
        text = str(coupon.get("description") or coupon.get("rewardText") or "").strip()
        if not text:
            continue
        limit = coupon.get("limitText")
        notes.append(f"{text} ({limit})" if limit else text)
    return notes
