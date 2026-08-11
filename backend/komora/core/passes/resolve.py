"""Resolution pass: descriptions in, real Silpo SKUs out.

This is where the read/write split pays off. The LLM only ever produces descriptions
("молоко 2,6%"); choosing an actual product, checking stock and handling substitutions
is deterministic code that can be tested without a model.

Three facts confirmed against the live server (spec 3.1) shape it:

* `find_products_batch` needs a `SearchContext` - branch, delivery type and timeslot.
  A bare query string is not enough.
* A search result names the product `id`; the cart calls the same value `productId`.
* Quantities must respect `stock` and the per-product `step`.
"""

from decimal import Decimal
from typing import Any

from komora.core.mcp.protocol import SilpoClient
from komora.core.models import (
    DraftBasket,
    ReasonKind,
    ResolvedCart,
    ResolvedLine,
    SearchContext,
)

MAX_QUERIES_PER_BATCH = 30
"""Silpo's documented ceiling for find_products_batch."""

NOT_FOUND = "not_found"
DEGRADED_REPLACEMENTS = "degraded:replacements"

# Silpo's tool descriptions are explicit: never re-add carrier bags.
_PLASTIC_BAGS = ("пакет", "пакунок")


def _is_plastic_bag(product: dict[str, Any]) -> bool:
    name = str(product.get("name", "")).casefold()
    return any(word in name for word in _PLASTIC_BAGS)


def _usable(product: dict[str, Any]) -> bool:
    return bool(product.get("id") and product.get("companyId") and not _is_plastic_bag(product))


def _in_stock(product: dict[str, Any]) -> bool:
    stock = product.get("stock")
    return bool(product.get("available", True)) and (stock is None or stock > 0)


def flatten_search(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """Search results arrive grouped per query: {"queries": [{"query", "products"}]}."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(payload, dict):
        return grouped
    for group in payload.get("queries") or []:
        if isinstance(group, dict):
            products = [p for p in (group.get("products") or []) if isinstance(p, dict)]
            grouped[str(group.get("query", ""))] = products
    return grouped


def clamp_quantity(wanted: float, product: dict[str, Any]) -> float:
    """Never exceed stock, and land on a multiple of the product's step.

    Silpo's own tool description is emphatic about the stock ceiling; `step` matters
    for weighted goods, where 1.0 may not be an orderable amount.
    """
    step = product.get("step") or 1
    stock = product.get("stock")
    capped = min(wanted, float(stock)) if stock is not None else float(wanted)
    if step and step > 0:
        steps = max(1, round(capped / step))
        capped = steps * step
        if stock is not None:
            capped = min(capped, float(stock))
    return round(capped, 3)


def _line_from(
    product: dict[str, Any],
    *,
    qty: float,
    reason_kind: ReasonKind,
    reason_text: str,
    optional: bool,
    substituted_from: str | None = None,
    unavailable: bool = False,
) -> ResolvedLine:
    old = product.get("oldPrice")
    return ResolvedLine(
        product_id=str(product["id"]),  # search says `id`; the cart wants `productId`
        company_id=str(product["companyId"]),
        branch_id=str(product.get("branchId", "")),
        name=str(product.get("name", "")),
        qty=qty,
        unit=str(product.get("ratio") or ""),
        unit_price=Decimal(str(product.get("price", 0))),
        old_price=Decimal(str(old)) if old else None,
        reason_kind=reason_kind,
        reason_text=reason_text,
        substituted_from=substituted_from,
        optional=optional,
        unavailable=unavailable,
    )


async def resolve_basket(
    basket: DraftBasket, mcp: SilpoClient, context: SearchContext
) -> ResolvedCart:
    """Turn a DraftBasket into a ResolvedCart of real products."""
    descriptions = [line.description for line in basket.lines]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(descriptions), MAX_QUERIES_PER_BATCH):
        chunk = descriptions[start : start + MAX_QUERIES_PER_BATCH]
        grouped |= flatten_search(await mcp.find_products_batch(chunk, context))

    lines: list[ResolvedLine] = []
    warnings = list(basket.warnings)

    for draft in basket.lines:
        candidates = [p for p in grouped.get(draft.description, []) if _usable(p)]
        if not candidates:
            warnings.append(f"{NOT_FOUND}:{draft.description}")
            continue

        available = next((p for p in candidates if _in_stock(p)), None)
        if available is not None:
            lines.append(
                _line_from(
                    available,
                    qty=clamp_quantity(draft.quantity, available),
                    reason_kind=draft.reason_kind,
                    reason_text=draft.reason_text,
                    optional=draft.optional,
                )
            )
            continue

        original = candidates[0]
        substitute, degraded = await _find_substitute(mcp, original, context)
        if degraded:
            warnings.append(DEGRADED_REPLACEMENTS)
        if substitute is not None:
            lines.append(
                _line_from(
                    substitute,
                    qty=clamp_quantity(draft.quantity, substitute),
                    reason_kind="sub",
                    reason_text="заміна — оригіналу немає в наявності",
                    optional=draft.optional,
                    substituted_from=str(original.get("name", "")),
                )
            )
        else:
            # Kept visible so the user sees what is missing; excluded from the total.
            lines.append(
                _line_from(
                    original,
                    qty=draft.quantity,
                    reason_kind=draft.reason_kind,
                    reason_text="немає в наявності",
                    optional=draft.optional,
                    unavailable=True,
                )
            )

    total = sum((line.line_total for line in lines if not line.unavailable), Decimal("0"))
    return ResolvedCart(lines=lines, total=total, warnings=warnings)


async def _find_substitute(
    mcp: SilpoClient, original: dict[str, Any], context: SearchContext
) -> tuple[dict[str, Any] | None, bool]:
    """Ask Silpo for a replacement. A failure here degrades the cart, never fails it."""
    try:
        payload = await mcp.get_replacements(
            product_ids=[str(original["id"])],
            company_id=str(original["companyId"]),
            context=context,
        )
    except Exception:
        return None, True

    for item in (payload or {}).get("items") or []:
        for candidate in item.get("replacements") or []:
            if _usable(candidate) and _in_stock(candidate):
                return candidate, False
    return None, False
