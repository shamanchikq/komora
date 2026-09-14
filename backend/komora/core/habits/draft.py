"""Due habits become the lines of a basket nobody typed.

No model request: every line is a product the user has bought, and its reason is the
cadence sentence the engine built. From here the lines take the *known-product* resolve
path (`passes/resolve.resolve_known`) and then the ordinary pipeline, so stock, price,
substitution and budget are handled exactly as for a typed basket — and it is confirmed
exactly like one.
"""

from __future__ import annotations

import math
from datetime import date

from komora.core.habits.engine import Habit
from komora.core.models import KnownLine

HABITS_TITLE = "Звичні покупки"
HABITS_INTENT = "habits"


def suggested_qty(habit: Habit) -> float:
    """The median of what was bought per day — whole packs for piece goods, three
    decimals of a kilogram for weighted ones, never zero."""
    if habit.weighted:
        return max(0.1, round(habit.median_qty, 3))
    return float(max(1, math.ceil(habit.median_qty - 1e-9)))


def habit_lines(habits: list[Habit], today: date) -> list[KnownLine]:
    """One line per reorderable habit, in the order given (soonest due first)."""
    return [
        KnownLine(
            product_id=habit.product_key,
            name=habit.name,
            quantity=suggested_qty(habit),
            external_product_id=habit.external_product_id,
            reason_kind="habit",
            reason_text=habit.sentence(today),
        )
        for habit in habits
        if habit.reorderable and not habit.muted
    ]
