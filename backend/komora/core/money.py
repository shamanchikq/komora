"""How an amount is written, in one place.

`Decimal` arithmetic keeps whatever scale its operands had, so a discount computed from
`(62.99 - 47.99) * 1` renders as "15.000". That reached a live run as
«знижка 15.000 ₴» — the kind of thing no unit test notices because nobody writes an
assertion about trailing zeros.

Lives in `core/` because the savings notes are built by a pass and stored in the
database; `bot/render.py` formats the same way by calling this.
"""

from decimal import ROUND_HALF_UP, Decimal

CURRENCY = "₴"


def uah(amount: Decimal) -> str:
    """Ukrainian convention: two decimals, a comma, and the sign after the number."""
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:.2f}".replace(".", ",") + f" {CURRENCY}"
