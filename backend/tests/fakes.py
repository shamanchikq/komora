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

    Cart writes carry only the four fields the schema declares, so — as on the real
    server — the name and price of a line come from the catalogue, not the request.
    """

    def __init__(
        self,
        results: dict[str, list[dict[str, Any]]] | None = None,
        replacements: dict[str, list[dict[str, Any]]] | None = None,
        *,
        replacements_fail: bool = False,
        existing: list[dict[str, Any]] | None = None,
        reject: set[str] | None = None,
        swallow: set[str] | None = None,
        checkout_links: bool = True,
        validations: list[dict[str, Any]] | None = None,
        coupons: list[dict[str, Any]] | None = None,
        coupon_details: dict[int, dict[str, Any]] | None = None,
        restrictions: Any = None,
        category_products: list[dict[str, Any]] | None = None,
        categories: list[dict[str, Any]] | None = None,
        slots: list[dict[str, Any]] | None = None,
        fails: set[str] | None = None,
    ) -> None:
        self._results = results or {}
        self._replacements = replacements or {}
        self._replacements_fail = replacements_fail
        self.search_calls: list[list[str]] = []
        self.replacement_calls: list[list[str]] = []

        self._cart: list[dict[str, Any]] = list(existing or [])
        self._reject = reject or set()
        self._swallow = swallow or set()
        """Accepted with a success response, then silently not added — the failure mode
        that makes trusting the write response instead of re-reading unsafe."""
        self._checkout_links = checkout_links
        self._validations = validations or []
        self._coupons = coupons or []
        self._coupon_details = coupon_details or {}
        self._restrictions = restrictions
        self._category_products = category_products
        self._categories = categories or []
        self.category_calls: list[str] = []
        self.category_pages: list[tuple[int, int]] = []
        self._slots = slots
        self._fails = fails or set()
        self.add_calls: list[list[dict[str, Any]]] = []
        self.remove_calls: list[list[dict[str, Any]]] = []
        self.write_order: list[str] = []
        """Cart writes in the order they happened — the per-method lists cannot show
        that adds run before removals, which is the property that decides whether a
        half-finished sync leaves the user with too much or too little."""

        self._catalogue: dict[str, dict[str, Any]] = {}
        for group in (*self._results.values(), *self._replacements.values()):
            for item in group:
                self._catalogue[str(item["id"])] = item
        for line in self._cart:
            self._catalogue.setdefault(str(line["productId"]), line)

    def _fail_if_scripted(self, name: str) -> None:
        if name in self._fails:
            raise RuntimeError(f"{name} unavailable")

    # --- cart ---
    async def get_my_shopping_cart(self) -> dict[str, Any]:
        self._fail_if_scripted("get_my_shopping_cart")
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
        self.write_order.append("add")
        rejected = [p for p in products if p["productId"] in self._reject]
        if rejected:
            raise RuntimeError(f"Silpo rejected {rejected[0]['productId']}")
        for item in products:
            if item["productId"] in self._swallow:
                continue
            known = self._catalogue.get(str(item["productId"]), {})
            existing = next((p for p in self._cart if p["productId"] == item["productId"]), None)
            if existing is None:
                self._cart.append(
                    {
                        "productId": item["productId"],
                        "companyId": item["companyId"],
                        "branchId": item["branchId"],
                        "name": known.get("name", item["productId"]),
                        "price": known.get("price", 0),
                        "quantity": item["quantity"],
                    }
                )
            else:
                # SET, not increment — the behaviour verified against the live server.
                existing["quantity"] = item["quantity"]
        return {"success": True}

    async def remove_cart_products(
        self, shopping_cart_id: str, products: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        """Its schema declares `productId` alone, so the fake reads nothing else — a
        caller that still sends quantity would pass here and fail on the real server."""
        self.remove_calls.append([dict(p) for p in products])
        self.write_order.append("remove")
        rejected = [p for p in products if p["productId"] in self._reject]
        if rejected:
            raise RuntimeError(f"Silpo refused to remove {rejected[0]['productId']}")
        removing = {p["productId"] for p in products}
        self._cart = [p for p in self._cart if p["productId"] not in removing]
        return {"success": True}

    # --- reads ---
    async def find_products_batch(
        self, queries: Sequence[str], context: SearchContext
    ) -> dict[str, Any]:
        self.search_calls.append(list(queries))
        self._fail_if_scripted("find_products_batch")
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

    async def get_products(self, context: SearchContext, **filters: Any) -> dict[str, Any]:
        """Category browse. Shape captured live — same product fields as a search hit,
        so nothing downstream cares which one a candidate came from."""
        self._fail_if_scripted("get_products")
        category = filters.get("category")
        if category is not None:
            self.category_calls.append(str(category))
        products = self._category_products if self._category_products is not None else []
        return {
            "success": True,
            "summary": f"Found {len(products)} products",
            "products": products,
            "meta": {"total": len(products)},
        }

    async def get_product_details(self, slug: str, context: SearchContext) -> dict[str, Any]:
        found = next((p for p in self._catalogue.values() if p.get("slug") == slug), None)
        return {"success": True, "product": found}

    async def get_promotions(self, context: SearchContext) -> dict[str, Any]:
        return {"success": True, "promotions": []}

    async def get_categories(self, context: SearchContext, **filters: Any) -> dict[str, Any]:
        """Paginates like the real endpoint, whose `limit` caps at 1000 while the tree
        is larger — the reason `fetch_categories` exists."""
        self._fail_if_scripted("get_categories")
        limit = int(filters.get("limit", 1000))
        offset = int(filters.get("offset", 0))
        page = self._categories[offset : offset + limit]
        self.category_pages.append((offset, limit))
        return {
            "success": True,
            "summary": f"Found {len(page)} categories (total: {len(self._categories)})",
            "categories": page,
            "meta": {"limit": limit, "offset": offset, "total": len(self._categories)},
        }

    async def get_my_coupons(self) -> dict[str, Any]:
        """Envelope captured live: `{"success", "summary", "coupons": [...]}`."""
        self._fail_if_scripted("get_my_coupons")
        return {
            "success": True,
            "summary": f"Found {len(self._coupons)} coupons",
            "coupons": self._coupons,
        }

    async def get_my_food_restrictions(self) -> dict[str, Any]:
        """Envelope captured live: `{"success", "summary", "restrictions": [...]}`."""
        self._fail_if_scripted("get_my_food_restrictions")
        if self._restrictions is not None:
            return self._restrictions
        return {"success": True, "summary": "No food restrictions set", "restrictions": []}

    async def get_coupon_details(self, business_coupon_id: int) -> dict[str, Any]:
        self._fail_if_scripted("get_coupon_details")
        return {"success": True, "coupon": self._coupon_details.get(business_coupon_id)}

    async def get_time_slots(
        self,
        *,
        branch_id: str,
        delivery_type: str,
        start: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Shape captured live: `{"success", "summary", "slots": [...], "meta"}`.

        Models the behaviour that caused a false positive on the live run: **without
        `start`, the window begins at the start of the current day**, so slots that
        have already passed come back with `available: false`. Callers that pass the
        slot they care about get a window beginning there.
        """
        self._fail_if_scripted("get_time_slots")
        slots = self._slots if self._slots is not None else list(self._default_slots)
        if start is not None:
            slots = [s for s in slots if str(s.get("start", "")) >= start]
        available = sum(1 for s in slots if s.get("available"))
        return {
            "success": True,
            "summary": f"Found {len(slots)} time slots ({available} available)",
            "slots": slots[:limit],
            "meta": {"total": len(slots)},
        }

    @property
    def _default_slots(self) -> list[dict[str, Any]]:
        """A passed slot, then the cart's own — the real shape of an evening response."""
        return [
            {
                "start": "2026-08-11T06:00:00+00:00",
                "end": "2026-08-11T06:30:00+00:00",
                "available": False,
                "deliveryType": CONTEXT.delivery_type,
            },
            {
                "start": CONTEXT.timeslot_start,
                "end": CONTEXT.timeslot_end,
                "available": True,
                "deliveryType": CONTEXT.delivery_type,
            },
        ]

    async def list_tools(self) -> list[dict[str, Any]]:
        return []
