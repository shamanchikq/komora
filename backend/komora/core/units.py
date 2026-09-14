"""Package sizes — `displayRatio` parsed, and a stated need turned into packs.

Since 2026-09-14 every product shape carries `displayRatio`, the content of one unit
(«900г», «0,5л», «10 шт»; «100г» on a weighted good, whose `quantity` stays in
kilograms) — reference §10.3. It is a string with a decimal comma and mixed units, and
the schema allows `null`. Everything here is defensive on purpose: a value it cannot
read is `None`, and a line with `None` keeps whatever quantity the model chose. The
narrow miss is a pack count the user corrects with the stepper; the wide miss was a
size guessed from a string nobody had measured.
"""

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

Unit = Literal["g", "ml", "pcs"]

_AMOUNT = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([^\d\s]+)\s*$")

_UNITS: dict[str, tuple[Unit, float]] = {
    # to grams
    "г": ("g", 1),
    "гр": ("g", 1),
    "кг": ("g", 1000),
    "g": ("g", 1),
    "kg": ("g", 1000),
    # to millilitres
    "мл": ("ml", 1),
    "л": ("ml", 1000),
    "ml": ("ml", 1),
    "l": ("ml", 1000),
    # pieces
    "шт": ("pcs", 1),
    "шт.": ("pcs", 1),
    "pcs": ("pcs", 1),
}
"""Only forms actually seen or their obvious singular spellings. An unknown unit is a
`None`, never a guess — «2*100г» and «<=0,5» (the details' bucket) both parse to
nothing, which is correct: neither is one pack's content."""


@dataclass(frozen=True)
class PackSize:
    amount: float
    """In base units: grams, millilitres or pieces."""
    unit: Unit


def parse_display_ratio(value: Any) -> PackSize | None:
    """«900г» → 900 g; «0,5л» → 500 ml; «10 шт» → 10 pcs; anything else → `None`."""
    if not isinstance(value, str):
        return None
    match = _AMOUNT.match(value)
    if match is None:
        return None
    number, unit = match.group(1).replace(",", "."), match.group(2).casefold()
    known = _UNITS.get(unit)
    if known is None:
        return None
    try:
        amount = float(number)
    except ValueError:
        return None
    if amount <= 0:
        return None
    return PackSize(amount=amount * known[1], unit=known[0])


def parse_need(value: float, unit: str) -> PackSize | None:
    """The model's `amount` field — «1,5 кг» as `(1.5, "кг")` — in the same base units."""
    known = _UNITS.get(unit.strip().casefold())
    if known is None or not math.isfinite(value) or value <= 0:
        return None
    return PackSize(amount=value * known[1], unit=known[0])


def packs_for(need: PackSize, pack: PackSize) -> float | None:
    """How many packs cover the need. `None` when the two are not comparable.

    Rounded **up** to a whole pack: a recipe that needs 1,2 kg of potatoes is not
    served by one 1 kg bag. Grams and millilitres are deliberately not converted into
    each other — the density is unknown and a wrong guess here is money.
    """
    if need.unit != pack.unit or pack.amount <= 0:
        return None
    return float(max(1, math.ceil(need.amount / pack.amount - 1e-9)))


def kilograms_for(need: PackSize) -> float | None:
    """A weighted good is ordered in kilograms whatever its `displayRatio` says."""
    if need.unit != "g":
        return None
    return round(need.amount / 1000, 3)
