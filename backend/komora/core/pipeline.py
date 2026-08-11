"""Draft basket in, reviewed cart out.

The passes are separate functions so each can be tested alone; this composes them in
the one order that works. Restrictions run on *descriptions*, before anything is
resolved, so an excluded item never costs a search. Savings and budget run last,
because both need real prices.

Everything Silpo can fail to answer degrades rather than aborts: a cart with no coupon
notes is worth having, a cart the user never sees is not. Each degradation leaves a
warning behind, and `bot/render.py` shows it — no failure is swallowed.
"""

from decimal import Decimal
from typing import Any

from komora.core.mcp.errors import McpError
from komora.core.mcp.protocol import SilpoClient
from komora.core.models import DraftBasket, ResolvedCart, SearchContext
from komora.core.passes.budget import apply_budget
from komora.core.passes.promos import DEGRADED_COUPONS, apply_savings, describe_coupons
from komora.core.passes.resolve import resolve_basket
from komora.core.passes.restrictions import apply_restrictions

DEGRADED_RESTRICTIONS = "degraded:restrictions"


class CartContextMissing(McpError):
    """The user's Silpo cart carries no branch or delivery slot.

    Not a bug and not recoverable from here: product search requires both, and only
    the user can choose them — in the Silpo app. Distinct from a transport failure so
    the bot can say something more useful than "спробуйте пізніше".
    """


def _cart_body(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        inner = payload.get("cart")
        return inner if isinstance(inner, dict) else payload
    return {}


async def load_context(mcp: SilpoClient) -> tuple[str, SearchContext]:
    """Read the cart, which is the only place the search context exists.

    Silpo's own docs call this "always the first step": `find_products_batch` requires
    branch, delivery type and timeslot, and all four live on the cart
    (`shipments[0].branchId`, `deliveryType`, `timeslot.start/.end`).
    """
    cart_id = str((await mcp.get_my_shopping_cart()).get("shoppingCartId") or "")
    if not cart_id:
        raise CartContextMissing("Silpo returned no shoppingCartId")

    cart = _cart_body(await mcp.get_shopping_cart_by_id(cart_id))
    shipments = [s for s in (cart.get("shipments") or []) if isinstance(s, dict)]
    timeslot = cart.get("timeslot") or {}

    fields = {
        "branch_id": shipments[0].get("branchId") if shipments else None,
        "delivery_type": cart.get("deliveryType"),
        "timeslot_start": timeslot.get("start"),
        "timeslot_end": timeslot.get("end"),
    }
    missing = [name for name, value in fields.items() if not value]
    if missing:
        raise CartContextMissing(f"cart {cart_id} has no {', '.join(missing)}")

    return cart_id, SearchContext.model_validate(fields)


def _listed(payload: Any, *keys: str) -> list[Any]:
    """Pull a list out of a response whose envelope we have not captured.

    Every Silpo response seen so far wraps its payload under a name, but neither
    `get_my_coupons` nor `get_my_food_restrictions` has been observed with real data —
    both are empty on the account we verified against. Rather than guess one shape,
    accept the plausible ones and treat anything else as empty.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def extract_restrictions(payload: Any) -> list[str]:
    """Restriction terms, from whichever shape arrived.

    `apply_restrictions` matches lexically on Ukrainian words, so a term is whatever
    string names the restriction.
    """
    terms: list[str] = []
    for item in _listed(payload, "restrictions", "items", "data", "result"):
        if isinstance(item, str):
            terms.append(item)
        elif isinstance(item, dict):
            for key in ("name", "title", "value", "restriction"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    terms.append(value)
                    break
    return [term.strip() for term in terms if term.strip()]


async def build_cart(
    basket: DraftBasket,
    mcp: SilpoClient,
    context: SearchContext,
    *,
    budget_cap: int | None = None,
    already_spent: Decimal | None = None,
) -> ResolvedCart:
    """Run the full pipeline: restrictions -> resolve -> savings -> budget."""
    warnings: list[str] = []

    try:
        restrictions = extract_restrictions(await mcp.get_my_food_restrictions())
    except Exception:
        restrictions = []
        warnings.append(DEGRADED_RESTRICTIONS)

    filtered = apply_restrictions(basket, restrictions)
    cart = await resolve_basket(filtered, mcp, context)
    cart = apply_savings(cart)

    try:
        notes = describe_coupons(_listed(await mcp.get_my_coupons(), "coupons", "items", "data"))
    except Exception:
        notes = []
        warnings.append(DEGRADED_COUPONS)

    cart = cart.model_copy(
        update={
            "savings_notes": [*cart.savings_notes, *notes],
            "warnings": [*cart.warnings, *warnings],
        }
    )
    return apply_budget(cart, budget_cap, already_spent)
