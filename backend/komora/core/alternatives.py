"""The next product Silpo would have offered for a line.

«Інший варіант» re-runs the line's own query rather than storing every candidate at
draft time. Silpo is not the rate-limited resource — the model is — so a search is the
cheap way to do this, and it avoids a schema that would have to hold a snapshot of the
catalogue alongside every basket.

Cycling wraps: the last alternative leads back to the first, so a user who taps past
the one they wanted comes round again rather than getting stuck.
"""

from typing import Any

from komora.core.mcp.protocol import SilpoClient
from komora.core.models import ResolvedLine, SearchContext
from komora.core.passes.categories import CategoryIndex
from komora.core.passes.resolve import (
    CATEGORY_PAGE,
    DEFAULT_QUANTITY,
    clamp_quantity,
    deliberate_quantity,
    fallback_terms,
    flatten_search,
    in_stock,
    line_from,
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


def _requantified(line: ResolvedLine, chosen: dict[str, Any]) -> float:
    """The line's quantity, carried onto another product.

    Same unit on both sides — kilograms to kilograms, pieces to pieces — and the
    amount is one somebody already settled: resolution turned the model's bare `1`
    into a step long before a swap. Re-running `clamp_quantity` on it read a kilo of
    potatoes the user had set with the stepper as that bare `1`, and swapped in 100 g.

    Across units the number means nothing on the other side, so the new product gets
    what an unqualified request gets: two packs of cheese are not two kilograms of
    the weighted one.
    """
    if line.weighted == bool(chosen.get("weighted")):
        return deliberate_quantity(line.qty, chosen)
    if chosen.get("weighted"):
        return clamp_quantity(DEFAULT_QUANTITY, chosen)
    return clamp_quantity(line.qty, chosen)


def _apply(line: ResolvedLine, chosen: dict[str, Any]) -> ResolvedLine:
    """This line, holding that product. What the user chose about the line — its
    quantity, reason, description, category, whether it is optional — survives; every
    fact about the *product* is the new product's.

    Only the price and the name used to change. `weighted`, `step`, `stock`, the pack
    size and the multi-buy prices stayed the old product's, so a weighted cheese
    swapped for a packaged one kept its «₴/кг» row, its 0,1 kg stepper and the old
    product's stock as a ceiling, and a note offered the old product's «від 2 шт» price
    under the new one's name.

    `substituted_from` is dropped: a chosen product was not swapped in for an
    out-of-stock original, and keeping the marker would caption it «Заміна замість …»
    wrongly. `synced` is dropped too — the new product is not in the Silpo cart.
    """
    return line_from(
        chosen,
        description=line.description,
        category=line.category,
        qty=_requantified(line, chosen),
        reason_kind=line.reason_kind,
        reason_text=line.reason_text,
        optional=line.optional,
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
