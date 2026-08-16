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

from komora.core.llm.protocol import LLMClient
from komora.core.mcp.errors import McpError
from komora.core.mcp.protocol import SilpoClient
from komora.core.models import DraftBasket, ResolvedCart, ResolvedLine, SearchContext
from komora.core.passes.budget import apply_budget
from komora.core.passes.categories import CategoryIndex, fetch_categories
from komora.core.passes.promos import DEGRADED_COUPONS, apply_savings, describe_coupons
from komora.core.passes.resolve import NOT_FOUND, resolve_basket
from komora.core.passes.restrictions import apply_restrictions
from komora.core.passes.verify import DEGRADED_VERIFY, find_mismatches

DEGRADED_RESTRICTIONS = "degraded:restrictions"
TIMESLOT_EXPIRED = "timeslot:expired"

MAX_COUPON_DETAILS = 5
"""Each one is a separate call, and a note nobody reads is not worth a round trip."""


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


def slot_verdict(payload: Any, cart_start: str) -> bool | None:
    """Is the cart's timeslot still on offer? `None` when it cannot be told.

    Silpo returns past slots with `available: false` — its own description says *"Only
    pick slots where available=true"* — so presence in the list proves nothing and only
    the flag counts.

    This is asked with `start` set to the cart's own slot, so the window begins exactly
    at the slot in question and a windowing miss cannot be mistaken for an expiry. The
    first version of this function asked without `start`, got back a window beginning at
    the start of the current day, and concluded — at 23:47 UTC, with every slot in it
    passed — that a perfectly valid cart had expired.
    """
    slots = [s for s in _listed(payload, "slots", "timeslots", "items") if isinstance(s, dict)]
    if not slots:
        return None
    return cart_start in {str(s["start"]) for s in slots if s.get("available") and s.get("start")}


async def timeslot_is_offered(mcp: SilpoClient, context: SearchContext) -> bool | None:
    """Silpo asks for this check immediately after reading the cart.

    A failure is never reported as "fine": an unanswerable question returns `None`, and
    only a definite `False` warns the user.
    """
    try:
        payload = await mcp.get_time_slots(
            branch_id=context.branch_id,
            delivery_type=context.delivery_type,
            start=context.timeslot_start,
            limit=25,
        )
    except Exception:
        return None
    return slot_verdict(payload, context.timeslot_start)


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


async def _coupons(mcp: SilpoClient) -> list[dict[str, Any]]:
    """The user's coupons, enriched with the value the list endpoint cannot carry.

    `get_my_coupons` is `additionalProperties: false` without `rewardValue`, so a
    coupon's own description can be a fragment — the real one on the test account reads
    just "на онлайн чек". `get_coupon_details` is the only place a number exists, and
    it costs one call per coupon, hence the cap.

    An enrichment failure keeps the plain coupon rather than dropping it.
    """
    listed = _listed(await mcp.get_my_coupons(), "coupons", "items", "data")
    active = [c for c in listed if isinstance(c, dict) and c.get("active")]

    enriched: list[dict[str, Any]] = []
    fetched = 0
    for coupon in active:
        coupon_id = coupon.get("id")
        # Counting `enriched` instead spent the budget on coupons that cost no call at
        # all: five id-less ones ahead of the rest meant nothing was ever enriched.
        if coupon_id is None or fetched >= MAX_COUPON_DETAILS:
            enriched.append(coupon)
            continue
        fetched += 1
        try:
            details = (await mcp.get_coupon_details(int(coupon_id))).get("coupon")
        except Exception:
            details = None
        enriched.append({**coupon, **details} if isinstance(details, dict) else coupon)
    return enriched


class SilpoCache:
    """Per-process cache for data that does not change during a conversation.

    The category tree is 1000 entries in one call. Fetching it per basket would be
    wasteful; fetching it per process is free after the first turn.
    """

    def __init__(self) -> None:
        self.categories: CategoryIndex | None = None
        self.categories_tried = False


async def categories_for(
    mcp: SilpoClient, context: SearchContext, cache: SilpoCache | None
) -> CategoryIndex | None:
    if cache is None:
        return None
    if not cache.categories_tried:
        cache.categories_tried = True
        try:
            cache.categories = CategoryIndex(await fetch_categories(mcp, context))
        except Exception:
            cache.categories = None
    return cache.categories


async def _verified(
    cart: ResolvedCart,
    basket: DraftBasket,
    mcp: SilpoClient,
    context: SearchContext,
    llm: LLMClient,
    cache: SilpoCache | None,
) -> tuple[ResolvedCart, bool]:
    """Drop or re-resolve lines the model says do not match the request.

    A flagged line is re-searched once with the model's suggested wording. If that
    still fails it becomes a `not_found` warning — a wrong product presented as the
    right one is the worst outcome available, because the user may simply buy it.
    """
    pairs = [(line.description or "", line.name) for line in cart.lines]
    mismatches = await find_mismatches(llm, pairs, basket.title)
    if mismatches is None:
        return cart, True
    if not mismatches:
        return cart, False

    originals = {line.description: line for line in basket.lines}
    retry = DraftBasket(
        title=basket.title,
        intent=basket.intent,
        lines=[
            # The category goes with it. A mismatch is evidence that the model's guess
            # at this line was wrong, and the category is part of that guess — keeping
            # it re-applies the constraint that produced the rejected product. Probed
            # live: «пармезан» under «Крафтові сири» yields three artisan cheeses and no
            # parmesan, so the retry returned another artisan cheese and the pass looked
            # useless. The model offered a better *query*, not a better shelf.
            (originals[cart.lines[i].description]).model_copy(
                update={"description": better, "category": None}
            )
            for i, better in mismatches.items()
            if better and cart.lines[i].description in originals
        ],
    )
    replacements = (
        {
            line.description: line
            for line in (
                await resolve_basket(retry, mcp, context, await categories_for(mcp, context, cache))
            ).lines
        }
        if retry.lines
        else {}
    )

    kept: list[ResolvedLine] = []
    warnings: list[str] = list(cart.warnings)
    for index, line in enumerate(cart.lines):
        if index not in mismatches:
            kept.append(line)
            continue
        better = mismatches[index]
        found = replacements.get(better)
        if found is not None:
            kept.append(found.model_copy(update={"description": line.description}))
        else:
            warnings.append(f"{NOT_FOUND}:{line.description}")

    total = sum((ln.line_total for ln in kept if not ln.unavailable), Decimal("0"))
    return cart.model_copy(update={"lines": kept, "total": total, "warnings": warnings}), False


async def build_cart(
    basket: DraftBasket,
    mcp: SilpoClient,
    context: SearchContext,
    *,
    budget_cap: int | None = None,
    already_spent: Decimal | None = None,
    llm: LLMClient | None = None,
    cache: SilpoCache | None = None,
) -> ResolvedCart:
    """Run the full pipeline: restrictions -> resolve -> verify -> savings -> budget.

    `llm` enables the verification pass — one extra request per basket, which is the
    scarce resource on the free tier. `cache` holds the category tree.
    """
    warnings: list[str] = []

    try:
        restrictions = extract_restrictions(await mcp.get_my_food_restrictions())
    except Exception:
        restrictions = []
        warnings.append(DEGRADED_RESTRICTIONS)

    filtered = apply_restrictions(basket, restrictions)
    cart = await resolve_basket(filtered, mcp, context, await categories_for(mcp, context, cache))
    if llm is not None:
        cart, degraded = await _verified(cart, filtered, mcp, context, llm, cache)
        if degraded:
            warnings.append(DEGRADED_VERIFY)
    cart = apply_savings(cart)

    try:
        notes = describe_coupons(await _coupons(mcp))
    except Exception:
        notes = []
        warnings.append(DEGRADED_COUPONS)

    if await timeslot_is_offered(mcp, context) is False:
        warnings.append(TIMESLOT_EXPIRED)

    cart = cart.model_copy(
        update={
            "coupon_notes": [*cart.coupon_notes, *notes],
            "warnings": [*cart.warnings, *warnings],
        }
    )
    return apply_budget(cart, budget_cap, already_spent)
