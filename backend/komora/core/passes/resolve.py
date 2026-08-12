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

import re
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
from komora.core.passes.categories import CategoryIndex

MAX_QUERIES_PER_BATCH = 30
"""Silpo's documented ceiling for find_products_batch."""

NOT_FOUND = "not_found"
DEGRADED_REPLACEMENTS = "degraded:replacements"

# Silpo's tool descriptions are explicit: never re-add carrier bags.
_PLASTIC_BAGS = ("пакет", "пакунок")


def _is_plastic_bag(product: dict[str, Any]) -> bool:
    name = str(product.get("name", "")).casefold()
    return any(word in name for word in _PLASTIC_BAGS)


def usable(product: dict[str, Any]) -> bool:
    return bool(product.get("id") and product.get("companyId") and not _is_plastic_bag(product))


def in_stock(product: dict[str, Any]) -> bool:
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


_ASIDE = re.compile(r"\([^)]*\)")
_MIN_WORD = 4
"""Shorter words ("для", "з", "та") match everything and rank nothing."""


def fallback_terms(description: str) -> list[str]:
    """Progressively simpler queries for a description Silpo matched nothing for.

    Measured live: «Ковбаса (наприклад, салямі або варена)» returns **0** products
    while «ковбаса» returns 30. The parenthetical aside is what kills it, and a model
    writing one is not a bug worth failing a basket over.
    """
    terms: list[str] = []
    seen = {description.casefold()}

    for candidate in (
        " ".join(_ASIDE.sub(" ", description).split()),
        " ".join(_ASIDE.sub(" ", description).split()).split(" ")[0],
    ):
        if candidate and candidate.casefold() not in seen:
            terms.append(candidate)
            seen.add(candidate.casefold())
    return terms


# A word-overlap score with a cheapest-wins tie-break was tried here and **reverted**.
#
# It was motivated by one observation: asked for «яйця», Silpo returns guinea fowl eggs
# at 257,40 ₴ ahead of hen's eggs at 59,39 ₴. But the same heuristic then resolved
# «кока кола» to chewy marmalade at 12,99 ₴ — Silpo writes the drink's name as
# "Coca-Cola" in Latin, so Cyrillic «кола» matches the candy and not the cola, and the
# candy is cheaper. Silpo's own ordering had it right.
#
# The eggs case turned out to be a symptom of a vague query, and it is fixed where it
# belongs: the prompt now asks for «яйця курячі», for which Silpo's first result is
# ordinary hen's eggs. Two alphabets in one catalogue defeat substring matching, and
# Silpo's relevance ranking is better than anything hand-rolled here.


DEFAULT_QUANTITY = 1.0
"""What the schema tells the model to send when the user named no amount."""


def clamp_quantity(wanted: float, product: dict[str, Any]) -> float:
    """Never exceed stock, and land on a multiple of the product's step.

    Silpo's own tool description is emphatic about the stock ceiling; `step` matters
    for weighted goods, where 1.0 may not be an orderable amount.

    **On a weighted product, quantity is kilograms.** `price` is per kilogram too, so
    an unqualified `1` orders a whole kilo: it put 2099 ₴ of 36-month Parmigiano into a
    carbonara basket, at a per-kilo price that was itself perfectly reasonable. Nobody
    asking for parmesan means a kilogram of it.

    So an unqualified quantity on a weighted good becomes one **step** — the smallest
    amount Silpo will sell, 100 g for that cheese and 250 g for sliced bacon. An
    explicit amount is still honoured: «2 кг картоплі» arrives as 2 and stays 2.
    The narrow miss is a user who really did want exactly 1 kg; they get 100 g and a
    swap button, which is the better failure of the two.
    """
    step = product.get("step") or 1
    if product.get("weighted") and wanted == DEFAULT_QUANTITY:
        wanted = float(step)
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
    description: str,
    qty: float,
    reason_kind: ReasonKind,
    reason_text: str,
    optional: bool,
    substituted_from: str | None = None,
    unavailable: bool = False,
) -> ResolvedLine:
    old = product.get("oldPrice")
    return ResolvedLine(
        description=description,
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


async def _search(
    mcp: SilpoClient, queries: list[str], context: SearchContext
) -> dict[str, list[dict[str, Any]]]:
    """Batched search, respecting Silpo's 30-query ceiling."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(queries), MAX_QUERIES_PER_BATCH):
        chunk = queries[start : start + MAX_QUERIES_PER_BATCH]
        grouped |= flatten_search(await mcp.find_products_batch(chunk, context))
    return grouped


def _usable_in(grouped: dict[str, list[dict[str, Any]]], term: str) -> list[dict[str, Any]]:
    return [p for p in grouped.get(term, []) if usable(p)]


async def _browse_categories(
    basket: DraftBasket,
    mcp: SilpoClient,
    context: SearchContext,
    categories: CategoryIndex | None,
) -> dict[str, list[dict[str, Any]]]:
    """Products from the category each line named, keyed by description.

    One call per distinct category — `get_products` takes a single one, unlike search.
    Silpo is not the rate-limited resource here (the model is), so this buys precision
    cheaply. A failure falls back to whatever search found.
    """
    if categories is None:
        return {}

    wanted: dict[str, str] = {}
    for line in basket.lines:
        slug = categories.slug_for(line.category)
        if slug:
            wanted[line.description] = slug

    results: dict[str, list[dict[str, Any]]] = {}
    by_slug: dict[str, list[dict[str, Any]]] = {}
    for description, slug in wanted.items():
        if slug not in by_slug:
            try:
                payload = await mcp.get_products(
                    context, category=slug, inStock=True, limit=MAX_QUERIES_PER_BATCH
                )
            except Exception:
                by_slug[slug] = []
            else:
                found = payload.get("products")
                by_slug[slug] = [p for p in found if isinstance(p, dict)] if found else []
        results[description] = by_slug[slug]
    return results


async def resolve_basket(
    basket: DraftBasket,
    mcp: SilpoClient,
    context: SearchContext,
    categories: CategoryIndex | None = None,
) -> ResolvedCart:
    """Turn a DraftBasket into a ResolvedCart of real products."""
    descriptions = [line.description for line in basket.lines]
    grouped = await _search(mcp, descriptions, context)
    browsed = await _browse_categories(basket, mcp, context, categories)

    # Second pass for anything that matched nothing. One extra batched call buys back
    # every line a parenthetical aside would otherwise have lost.
    retries = {d: fallback_terms(d) for d in descriptions if not _usable_in(grouped, d)}
    extra = list(dict.fromkeys(term for terms in retries.values() for term in terms))
    if extra:
        grouped |= await _search(mcp, extra, context)

    lines: list[ResolvedLine] = []
    warnings = list(basket.warnings)

    for draft in basket.lines:
        # A named category beats a search string: it is Silpo's own answer to "which
        # of these near-identical names did you mean".
        candidates = [p for p in browsed.get(draft.description, []) if usable(p)]
        if not candidates:
            candidates = _usable_in(grouped, draft.description)
        for term in retries.get(draft.description, []):
            if candidates:
                break
            candidates = _usable_in(grouped, term)

        if not candidates:
            warnings.append(f"{NOT_FOUND}:{draft.description}")
            continue

        available = next((p for p in candidates if in_stock(p)), None)
        if available is not None:
            lines.append(
                _line_from(
                    available,
                    description=draft.description,
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
                    description=draft.description,
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
                    description=draft.description,
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
            if usable(candidate) and in_stock(candidate):
                return candidate, False
    return None, False
