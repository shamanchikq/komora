"""A Silpo stand-in built from the real captured response shapes.

Product dicts mirror `tests/fixtures/mcp/find_products_batch.json` exactly — `id` (not
`productId`), `stock`, `available`, `step`, `oldPrice` — so a pass that works here
works against the live server.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from komora.core.models import SearchContext

CONTEXT = SearchContext(
    branch_id="1edb6b13-6640-6072-b10a-0b7012e7f9f8",
    delivery_type="SelfPickup",
    timeslot_start="2026-08-11T17:00:00+00:00",
    timeslot_end="2026-08-11T17:30:00+00:00",
)

COMPANY = "1ec88c5d-a050-669c-8467-570a157f3e31"


def product(
    name: str,
    price: float,
    *,
    product_id: str | None = None,
    stock: float | None = 10,
    available: bool = True,
    step: float = 1,
    old_price: float | None = None,
    ratio: str = "900г",
) -> dict[str, Any]:
    return {
        "id": product_id or f"id-{name}",
        "name": name,
        "slug": name.lower().replace(" ", "-"),
        "price": price,
        "oldPrice": old_price,
        "stock": stock,
        "available": available,
        "weighted": False,
        "step": step,
        "specialPrices": None,
        "companyId": COMPANY,
        "branchId": CONTEXT.branch_id,
        "ratio": ratio,
    }


CART_ID = "5bee1a17-07ac-4ec4-bb34-656fdc8d6bb1"


class FakeSilpo:
    """Records calls so tests can assert batching and arguments, not just results.

    The cart models Silpo's real behaviour, confirmed live (spec §3.1): adding
    **appends**, and re-adding a product **sets** its quantity rather than incrementing
    it. That second detail is what makes a retried sync idempotent, so a fake that
    incremented would make the idempotency tests meaningless.
    """

    def __init__(
        self,
        results: dict[str, list[dict[str, Any]]] | None = None,
        replacements: dict[str, list[dict[str, Any]]] | None = None,
        *,
        replacements_fail: bool = False,
        existing: list[dict[str, Any]] | None = None,
        reject: set[str] | None = None,
        checkout_links: bool = True,
        validations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._results = results or {}
        self._replacements = replacements or {}
        self._replacements_fail = replacements_fail
        self.search_calls: list[list[str]] = []
        self.replacement_calls: list[list[str]] = []

        self._cart: list[dict[str, Any]] = list(existing or [])
        self._reject = reject or set()
        self._checkout_links = checkout_links
        self._validations = validations or []
        self.add_calls: list[list[dict[str, Any]]] = []

    # --- cart ---
    async def get_my_shopping_cart(self) -> dict[str, Any]:
        return {"success": True, "shoppingCartId": CART_ID}

    async def get_shopping_cart_by_id(self, shopping_cart_id: str) -> dict[str, Any]:
        total = sum(Decimal(str(p["price"])) * Decimal(str(p["quantity"])) for p in self._cart)
        cart: dict[str, Any] = {
            "id": shopping_cart_id,
            "deliveryType": CONTEXT.delivery_type,
            "timeslot": {"start": CONTEXT.timeslot_start, "end": CONTEXT.timeslot_end},
            "shipments": [
                {
                    "id": "s1",
                    "companyId": COMPANY,
                    "branchId": CONTEXT.branch_id,
                    "products": [dict(p) for p in self._cart],
                }
            ],
            "calculation": {
                "total": float(total),
                # What the user actually pays — Silpo says always show this, not `total`.
                "totalAfterDiscounts": float(total),
                "validations": self._validations,
            },
        }
        if self._checkout_links:
            cart["checkoutWebLink"] = "https://silpo.ua/checkout/abc"
            cart["checkoutMobileLink"] = "silpo://checkout/abc"
        return {"success": True, "cart": cart}

    async def add_or_update_cart_products(
        self, shopping_cart_id: str, products: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        self.add_calls.append([dict(p) for p in products])
        rejected = [p for p in products if p["productId"] in self._reject]
        if rejected:
            raise RuntimeError(f"Silpo rejected {rejected[0]['productId']}")
        for item in products:
            existing = next((p for p in self._cart if p["productId"] == item["productId"]), None)
            if existing is None:
                self._cart.append(
                    {
                        "productId": item["productId"],
                        "companyId": item["companyId"],
                        "branchId": item["branchId"],
                        "name": item.get("name", item["productId"]),
                        "price": item.get("price", 10),
                        "quantity": item["quantity"],
                    }
                )
            else:
                # SET, not increment — the behaviour verified against the live server.
                existing["quantity"] = item["quantity"]
        return {"success": True}

    async def find_products_batch(
        self, queries: Sequence[str], context: SearchContext
    ) -> dict[str, Any]:
        self.search_calls.append(list(queries))
        return {
            "success": True,
            "queries": [
                {
                    "query": q,
                    "totalFound": len(self._results.get(q, [])),
                    "products": self._results.get(q, []),
                }
                for q in queries
            ],
        }

    async def get_replacements(
        self, *, product_ids: Sequence[str], company_id: str, context: SearchContext
    ) -> dict[str, Any]:
        self.replacement_calls.append(list(product_ids))
        if self._replacements_fail:
            raise RuntimeError("replacements unavailable")
        return {
            "success": True,
            "items": [
                {"productId": pid, "replacements": self._replacements.get(pid, [])}
                for pid in product_ids
            ],
        }
