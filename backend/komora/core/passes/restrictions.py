"""Dietary restriction pass.

Matching is lexical: a household's declared restrictions are short Ukrainian words
("арахіс", "лактоза") and the draft lines are descriptions in the same language.
Category-level matching needs product data the draft does not have yet, and belongs
after resolution.
"""

from collections.abc import Sequence

from komora.core.models import DraftBasket

EXCLUDED = "excluded"


def apply_restrictions(basket: DraftBasket, restrictions: Sequence[str]) -> DraftBasket:
    """Drop lines matching a declared restriction, recording why.

    The exclusion is always surfaced. A line that quietly disappears looks like a bug
    to the user, and for an allergy the reason matters more than the item.
    """
    active = [r.strip().casefold() for r in restrictions if r and r.strip()]
    if not active:
        return basket

    kept = []
    warnings = list(basket.warnings)
    for line in basket.lines:
        haystack = line.description.casefold()
        hit = next((r for r in active if r in haystack), None)
        if hit is None:
            kept.append(line)
        else:
            warnings.append(f"{EXCLUDED}:{line.description}:{hit}")

    return basket.model_copy(update={"lines": kept, "warnings": warnings})
