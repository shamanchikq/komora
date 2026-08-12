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
from komora.core.passes.resolve import (
    clamp_quantity,
    fallback_terms,
    flatten_search,
    in_stock,
    usable,
)


async def next_alternative(
    line: ResolvedLine, mcp: SilpoClient, context: SearchContext
) -> ResolvedLine | None:
    """The product after this one for the same query, or None if there is no choice.

    The line keeps its quantity, reason and description — only the product changes.
    A swapped line is never `unavailable`: every candidate considered is in stock.
    """
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


async def _candidates(query: str, mcp: SilpoClient, context: SearchContext) -> list[dict[str, Any]]:
    grouped = flatten_search(await mcp.find_products_batch([query], context))
    return [p for p in grouped.get(query, []) if usable(p) and in_stock(p)]
