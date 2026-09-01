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
    CATEGORY_PAGE,
    clamp_quantity,
    fallback_terms,
    flatten_search,
    in_stock,
    narrow,
    usable,
)

MAX_ALTERNATIVES = 5
"""How many other products a picker offers for one line.

Enough that the right one is usually on screen, few enough to read without scrolling
past the line being replaced. Beyond this the honest answer is a different search, not
a longer list — and each entry costs nothing extra, since they all come from the one
candidate list `next_alternative` already builds.
"""


async def candidates_for(
    line: ResolvedLine,
    mcp: SilpoClient,
    context: SearchContext,
    categories: CategoryIndex | None = None,
) -> list[dict[str, Any]]:
    """The ranked candidate list for one line — the whole of it, in `narrow` order.

    **Alternatives stay on the same shelf, in relevance order.** The category keeps a
    swap from walking off into a different aisle; the search decides which item on that
    aisle comes next. Letting the shelf answer on its own — which it did, whenever the
    category held two or more products — turned «⇄» into a tour of the whole category in
    Silpo's arbitrary order: asked for parmigiano, it offered cheese after unrelated
    cheese and never arrived. `narrow` is the same rule `resolve_basket` uses, because
    picking a product and picking the next one are the same question.

    Shared by the two things that ask it: cycling to the next product, and listing
    several to choose between. They differ only in how many of this list they take.
    """
    slug = categories.slug_for(line.category) if categories else None
    shelf = await _in_category(slug, mcp, context) if slug else []

    query = line.description.strip()
    found = await _candidates(query, mcp, context) if query else []
    for term in fallback_terms(query) if query else []:
        if found:
            break
        found = await _candidates(term, mcp, context)

    return narrow(found, shelf)


async def next_alternative(
    line: ResolvedLine,
    mcp: SilpoClient,
    context: SearchContext,
    categories: CategoryIndex | None = None,
) -> ResolvedLine | None:
    """The product after this one, or None if there is no choice.

    The line keeps its quantity, reason, description and category — only the product
    changes. A swapped line is never `unavailable`: every candidate is in stock.
    """
    candidates = await candidates_for(line, mcp, context, categories)
    if len(candidates) < 2:
        return None
    return _swapped(line, candidates)


async def list_alternatives(
    line: ResolvedLine,
    mcp: SilpoClient,
    context: SearchContext,
    categories: CategoryIndex | None = None,
    limit: int = MAX_ALTERNATIVES,
) -> list[ResolvedLine]:
    """Up to `limit` other products for this line, best first.

    The same candidates «⇄» cycles through, offered all at once instead. Cycling could
    only move forward — a user who tapped past the one they wanted had to go round the
    whole list to reach it again, and every tap was a fresh round trip to Silpo for a
    search that had already been made. The list was built and thrown away each time.

    The current product is excluded: it is not an alternative to itself, and the
    surface showing these already has it.
    """
    candidates = await candidates_for(line, mcp, context, categories)
    options: list[ResolvedLine] = []
    for product in candidates:
        if str(product.get("id")) == line.product_id:
            continue
        options.append(_apply(line, product))
        if len(options) >= limit:
            break
    return options


def _swapped(line: ResolvedLine, candidates: list[dict[str, Any]]) -> ResolvedLine | None:
    position = next(
        (i for i, p in enumerate(candidates) if str(p.get("id")) == line.product_id), -1
    )
    chosen = candidates[(position + 1) % len(candidates)]
    if str(chosen.get("id")) == line.product_id:
        return None
    return _apply(line, chosen)


def _apply(line: ResolvedLine, chosen: dict[str, Any]) -> ResolvedLine:
    """This line, holding that product. Everything the user chose about the line —
    quantity, reason, description, category — survives; only the product changes."""
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
            # A chosen product has not been swapped in for an out-of-stock original;
            # keeping the old marker would caption it «Заміна замість …» wrongly.
            "substituted_from": None,
        }
    )


async def _in_category(slug: str, mcp: SilpoClient, context: SearchContext) -> list[dict[str, Any]]:
    try:
        # Same page size as `resolve`: `narrow` decides its fallback by asking whether
        # the shelf came back full, so a different limit here would make it guess wrong.
        payload = await mcp.get_products(context, category=slug, inStock=True, limit=CATEGORY_PAGE)
    except Exception:
        return []
    products = payload.get("products") or []
    return [p for p in products if isinstance(p, dict) and usable(p) and in_stock(p)]


async def _candidates(query: str, mcp: SilpoClient, context: SearchContext) -> list[dict[str, Any]]:
    grouped = flatten_search(await mcp.find_products_batch([query], context))
    return [p for p in grouped.get(query, []) if usable(p) and in_stock(p)]
