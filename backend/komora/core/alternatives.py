"""The next product Silpo would have offered for a line.

«Інший варіант» re-runs the line's own query rather than storing every candidate at
draft time. Silpo is not the rate-limited resource — the model is — so a search is the
cheap way to do this, and it avoids a schema that would have to hold a snapshot of the
catalogue alongside every basket.

Cycling wraps: the last alternative leads back to the first, so a user who taps past
the one they wanted comes round again rather than getting stuck.
"""

from decimal import Decimal
from typing import Any

from komora.core.mcp.protocol import SilpoClient
from komora.core.models import ResolvedLine, SearchContext
from komora.core.passes.categories import CategoryIndex
from komora.core.passes.resolve import (
    clamp_quantity,
    fallback_terms,
    flatten_search,
    in_stock,
    usable,
)


async def next_alternative(
    line: ResolvedLine,
    mcp: SilpoClient,
    context: SearchContext,
    categories: CategoryIndex | None = None,
) -> ResolvedLine | None:
    """The product after this one, or None if there is no choice.

    The line keeps its quantity, reason, description and category — only the product
    changes. A swapped line is never `unavailable`: every candidate is in stock.

    **Alternatives come from the same shelf.** If the line was resolved by browsing a
    category, so is its replacement; falling back to free-text here would walk off into
    a different category and undo the disambiguation that put the line there.
    """
    slug = categories.slug_for(line.category) if categories else None
    if slug:
        candidates = await _in_category(slug, mcp, context)
        if len(candidates) >= 2:
            return _swapped(line, candidates)

    query = line.description.strip()
    if not query:
        return None

    candidates = await _candidates(query, mcp, context)
    for term in fallback_terms(query):
        if candidates:
            break
        candidates = await _candidates(term, mcp, context)

    if len(candidates) < 2:
        return None
    return _swapped(line, candidates)


def _swapped(line: ResolvedLine, candidates: list[dict[str, Any]]) -> ResolvedLine | None:
    position = next(
        (i for i, p in enumerate(candidates) if str(p.get("id")) == line.product_id), -1
    )
    chosen = candidates[(position + 1) % len(candidates)]
    if str(chosen.get("id")) == line.product_id:
        return None

    old = chosen.get("oldPrice")
    return line.model_copy(
        update={
            "product_id": str(chosen["id"]),
            "company_id": str(chosen["companyId"]),
            "branch_id": str(chosen.get("branchId", "")),
            "name": str(chosen.get("name", "")),
            "unit": str(chosen.get("ratio") or ""),
            "unit_price": Decimal(str(chosen.get("price", 0))),
            "old_price": Decimal(str(old)) if old else None,
            "qty": clamp_quantity(line.qty, chosen),
            "unavailable": False,
        }
    )


async def _in_category(slug: str, mcp: SilpoClient, context: SearchContext) -> list[dict[str, Any]]:
    try:
        payload = await mcp.get_products(context, category=slug, inStock=True, limit=30)
    except Exception:
        return []
    products = payload.get("products") or []
    return [p for p in products if isinstance(p, dict) and usable(p) and in_stock(p)]


async def _candidates(query: str, mcp: SilpoClient, context: SearchContext) -> list[dict[str, Any]]:
    grouped = flatten_search(await mcp.find_products_batch([query], context))
    return [p for p in grouped.get(query, []) if usable(p) and in_stock(p)]
