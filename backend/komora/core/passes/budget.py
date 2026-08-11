"""Budget pass — flags an over-budget cart without silently editing it."""

from decimal import Decimal

from komora.core.models import ResolvedCart

OVER_BUDGET = "over_budget"


def apply_budget(
    cart: ResolvedCart, cap: int | None, already_spent: Decimal | None = None
) -> ResolvedCart:
    """Annotate the cart if it exceeds the user's weekly cap.

    Deliberately does not remove anything. Going over budget is the user's call to
    make — the UI offers to trim the optional lines and says so in as many words
    ("Це ваш вибір — надіслати можна попри це"). Silently dropping items the user
    asked for would be worse than the overage.
    """
    if cap is None:
        return cart

    used = cart.total + (already_spent or Decimal("0"))
    if used <= cap:
        return cart

    overage = used - cap
    return cart.model_copy(update={"warnings": [*cart.warnings, f"{OVER_BUDGET}:{overage}"]})


def optional_lines_total(cart: ResolvedCart) -> Decimal:
    """What trimming every optional line would save — the figure behind the
    "Прибрати N необов'язкових" button."""
    return sum(
        (line.line_total for line in cart.lines if line.optional and not line.unavailable),
        Decimal("0"),
    )
