"""Hand the reviewed draft to the user's real Silpo cart.

Two behaviours confirmed against the live server (spec §3.1) do most of the work here:

* **Adding appends.** The user's existing lines survive, which is the promise the
  confirmation sheet makes.
* **Re-adding a product sets its quantity rather than incrementing it.** That makes a
  retried sync idempotent by construction — the partial-failure path below relies on
  it — but it also means an overlapping product does *not* sum, so the sheet must not
  promise addition for something already in the cart.

Success is judged by re-reading the cart, not by trusting the write response. What is
actually in the cart afterwards is the only answer that matters to a user, and it makes
a partial failure impossible to mistake for success.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from komora.core.mcp.protocol import SilpoClient
from komora.core.models import (
    CartRemoval,
    ResolvedCart,
    ResolvedLine,
    SearchContext,
    SyncReport,
)
from komora.core.passes.resolve import flatten_search

DRIFT_TOLERANCE = Decimal("0.02")
"""Two percent. Below that, re-confirming costs the user more than it tells them."""


@dataclass(frozen=True)
class SyncPreview:
    existing_count: int
    existing_total: Decimal
    adding_count: int
    adding_total: Decimal
    overlapping: list[str] = field(default_factory=list)
    """Products already in the Silpo cart. Their quantity will be **replaced**."""
    now_unavailable: list[str] = field(default_factory=list)
    """Drafted products Silpo no longer offers. They are still sent — the cart write
    is the authority — but the user is told before confirming."""
    removing: list[str] = field(default_factory=list)
    """Products this confirmation takes OUT of the cart, checked against a fresh read.

    Anything the user has already removed themselves is dropped here rather than
    promised and then silently not done."""
    blocking_validations: list[str] = field(default_factory=list)
    """Errors from `cart.calculation.validations[]` — these stop checkout."""
    drift: tuple[Decimal, Decimal] | None = None
    """(confirmed total, current total) when prices moved more than the tolerance."""

    @property
    def final_count(self) -> int:
        return self.existing_count + self.adding_count - len(self.overlapping) - len(self.removing)


def _cart_body(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        inner = payload.get("cart")
        return inner if isinstance(inner, dict) else payload
    return {}


def _lines_of(payload: Any) -> list[dict[str, Any]]:
    """Cart lines live at cart.shipments[].products[]."""
    out: list[dict[str, Any]] = []
    for shipment in _cart_body(payload).get("shipments") or []:
        if isinstance(shipment, dict):
            out.extend(p for p in (shipment.get("products") or []) if isinstance(p, dict))
    return out


def _paid_total(payload: Any) -> Decimal:
    """`totalAfterDiscounts` is what the user actually pays; Silpo says never show
    `total` instead."""
    calculation = _cart_body(payload).get("calculation") or {}
    amount = calculation.get("totalAfterDiscounts", calculation.get("total", 0))
    return Decimal(str(amount or 0))


SUM_DEPENDENT = frozenset({"order.cost.min"})
"""Validations that describe the cart *total*, and so cannot be judged before a write.

Silpo computes `validations[]` against the cart as it stands right now. In a preview
that is the cart **without** the lines about to be added, so a minimum-order complaint
about an empty cart was shown to a user who was in the act of adding 2401 ₴ of food.
The same check after the write is meaningful, and there it correctly said nothing.
"""


def _blocking(payload: Any, *, adding: bool = False) -> list[str]:
    """Error-level entries from `calculation.validations[]`.

    These are the reason `checkoutWebLink` can be absent from an otherwise healthy
    cart: Silpo only issues one when the cart is actually checkout-ready. The values
    are **codes**, not prose — `bot/render.py: validation_text` translates them.

    `adding=True` drops the sum-dependent ones, which a pending addition may resolve.
    Everything else — an expired timeslot, a line over stock — is about what is already
    in the cart and stays true regardless.
    """
    calculation = _cart_body(payload).get("calculation") or {}
    codes = [
        str(v.get("message", ""))
        for v in (calculation.get("validations") or [])
        if str(v.get("level", "")).lower() == "error"
    ]
    return [code for code in codes if not (adding and code in SUM_DEPENDENT)]


def _sendable(cart: ResolvedCart) -> list[ResolvedLine]:
    return [line for line in cart.lines if not line.unavailable]


def _removable(cart: ResolvedCart, sendable: list[ResolvedLine]) -> list[CartRemoval]:
    """Removals this basket is not simultaneously adding.

    Adding a product and deleting it in the same confirmation would leave the cart in
    whichever state the two calls happened to finish in. The add wins: the user is
    looking at a draft that lists it.
    """
    adding = {line.product_id for line in sendable}
    return [removal for removal in cart.removals if removal.product_id not in adding]


def _payload(line: ResolvedLine) -> dict[str, Any]:
    """Exactly the four fields `silpo_add_or_update_cart_products` declares.

    `name` and `price` are not in its schema. The A1 probe that verified append
    semantics sent only these four, so anything extra here would be an unverified
    change to the one call the whole product depends on.
    """
    return {
        "productId": line.product_id,
        "companyId": line.company_id,
        "branchId": line.branch_id,
        "quantity": line.qty,
    }


async def _reprice(
    lines: list[ResolvedLine], mcp: SilpoClient, context: SearchContext | None
) -> dict[str, tuple[Decimal, bool]]:
    """product_id -> (current price, still on offer), from one batched search.

    Without this the drift check compared the stored total against the sum of the
    stored lines — the same number twice, so it could never fire while the preview
    implied that price changes were being watched for. A safeguard that cannot trigger
    is worse than none, because it is believed.

    Silpo is not the rate-limited resource, so re-asking costs a round trip and nothing
    else. A line whose description finds nothing keeps its drafted price rather than
    being called a price change.
    """
    if context is None or not lines:
        return {}

    queries = list(dict.fromkeys(line.description for line in lines if line.description))
    if not queries:
        return {}

    try:
        grouped = flatten_search(await mcp.find_products_batch(queries, context))
    except Exception:
        return {}

    found: dict[str, tuple[Decimal, bool]] = {}
    for products in grouped.values():
        for product in products:
            pid = str(product.get("id") or "")
            if pid:
                available = bool(product.get("available", True)) and (
                    product.get("stock") is None or (product.get("stock") or 0) > 0
                )
                found[pid] = (Decimal(str(product.get("price", 0))), available)
    return found


async def preview_sync(
    cart: ResolvedCart, mcp: SilpoClient, context: SearchContext | None = None
) -> SyncPreview:
    """Everything the confirmation sheet needs, read fresh from Silpo.

    `context` enables the live re-pricing; without it the preview still works and
    simply reports no drift, which is what every caller did before it existed.
    """
    cart_id = (await mcp.get_my_shopping_cart()).get("shoppingCartId", "")
    current = await mcp.get_shopping_cart_by_id(str(cart_id))

    existing = _lines_of(current)
    existing_ids = {str(p.get("productId")) for p in existing}
    adding = _sendable(cart)

    live = await _reprice(adding, mcp, context)
    live_total = sum(
        (
            live.get(line.product_id, (line.unit_price, True))[0] * Decimal(str(line.qty))
            for line in adding
        ),
        Decimal("0"),
    )
    gone = [line.name for line in adding if not live.get(line.product_id, (None, True))[1]]

    drift = None
    if cart.total > 0 and abs(live_total - cart.total) / cart.total > DRIFT_TOLERANCE:
        drift = (cart.total, live_total)

    return SyncPreview(
        existing_count=len(existing),
        existing_total=_paid_total(current),
        adding_count=len(adding),
        adding_total=live_total,
        overlapping=[line.name for line in adding if line.product_id in existing_ids],
        now_unavailable=gone,
        # Only what is demonstrably in the cart right now: the draft's removal list was
        # matched against what Komora *believes* it synced, and the user may have
        # emptied the cart or removed the item themselves since.
        removing=[r.name for r in _removable(cart, adding) if r.product_id in existing_ids],
        blocking_validations=_blocking(current, adding=bool(adding)),
        drift=drift,
    )


async def execute_sync(cart: ResolvedCart, mcp: SilpoClient) -> SyncReport:
    """Apply the draft to the user's cart and report what actually happened.

    Adds run before removals. If the two disagree — a cart write fails, the connection
    drops between them — the user is left holding too much rather than too little,
    which is the recoverable direction: an extra item costs two taps in the Silpo app,
    a deleted one has to be found and chosen again.
    """
    sendable = _sendable(cart)
    removals = _removable(cart, sendable)
    if not sendable and not removals:
        return SyncReport(ok=True)

    cart_id = str((await mcp.get_my_shopping_cart()).get("shoppingCartId", ""))
    errors: dict[str, str] = {}

    if sendable:
        try:
            await mcp.add_or_update_cart_products(cart_id, [_payload(line) for line in sendable])
        except Exception:
            for line in sendable:
                try:
                    await mcp.add_or_update_cart_products(cart_id, [_payload(line)])
                except Exception as exc:
                    errors[line.product_id] = str(exc)

    # Ground truth: what is in the cart now, not what the write call claimed.
    landed = {
        str(p.get("productId")) for p in _lines_of(await mcp.get_shopping_cart_by_id(cart_id))
    }
    added = [line.name for line in sendable if line.product_id in landed]
    failed = [
        (line.name, errors.get(line.product_id, "не додалося"))
        for line in sendable
        if line.product_id not in landed
    ]

    # A removal target that is no longer in the cart needs no call and is not a
    # failure — the user got what they asked for, by their own hand or ours.
    targets = [removal for removal in removals if removal.product_id in landed]
    remove_errors: dict[str, str] = {}
    if targets:
        try:
            await mcp.remove_cart_products(
                cart_id, [{"productId": removal.product_id} for removal in targets]
            )
        except Exception:
            for removal in targets:
                try:
                    await mcp.remove_cart_products(cart_id, [{"productId": removal.product_id}])
                except Exception as exc:
                    remove_errors[removal.product_id] = str(exc)

    final_payload = await mcp.get_shopping_cart_by_id(cart_id)
    final = _cart_body(final_payload)
    remaining = {str(p.get("productId")) for p in _lines_of(final_payload)}
    removed = [r.name for r in targets if r.product_id not in remaining]
    remove_failed = [
        (r.name, remove_errors.get(r.product_id, "не прибралося"))
        for r in targets
        if r.product_id in remaining
    ]

    return SyncReport(
        ok=not failed and not remove_failed,
        added=added,
        failed=failed,
        removed=removed,
        remove_failed=remove_failed,
        checkout_web_link=final.get("checkoutWebLink"),
        checkout_mobile_link=final.get("checkoutMobileLink"),
        # Why there may be no link. Observed live: a cart holding a line that exceeds
        # stock gets no `checkoutWebLink`, and saying "Готово" with no link and no
        # reason leaves the user with nowhere to go.
        blocking_validations=_blocking(final_payload),
    )
