"""The Silpo surface the rest of Komora depends on.

Everything downstream — the passes, the agent loop, sync — depends on this Protocol
rather than the concrete client, so the whole pipeline is testable without a network
or an OAuth flow.

Signatures follow the schemas captured from the live server, not the tool names. Two
that surprised us (spec §3.1): product search needs branch and delivery context rather
than a bare query, and cart operations key on `shoppingCartId`, not `cartId`.

Payloads stay loose `dict`s: only fields verified against a real response should be
read, and those are recorded in `tests/fixtures/mcp/`.
"""

from collections.abc import Sequence
from typing import Any, Protocol

from komora.core.models import SearchContext


class SilpoClient(Protocol):
    # --- Reads. Safe for the agent to call freely. ---
    async def find_products_batch(
        self, queries: Sequence[str], context: SearchContext
    ) -> dict[str, Any]:
        """Search up to 30 products at once.

        Returns `{"queries": [{"query", "totalFound", "products"}]}` — results are
        grouped per query, and each product is identified by `id` (the cart calls the
        same value `productId`).
        """
        ...

    async def get_products(self, context: SearchContext, **filters: Any) -> dict[str, Any]:
        """Browse by category or promotion code. Same context requirement as search."""
        ...

    async def get_product_details(self, slug: str, context: SearchContext) -> dict[str, Any]:
        """Full details for one product.

        `slug` must come from a search result — Silpo's own description says never to
        construct one from a name. Needs the same context as search.
        """
        ...

    async def get_replacements(
        self, *, product_ids: Sequence[str], company_id: str, context: SearchContext
    ) -> dict[str, Any]:
        """Substitutes for out-of-stock products.

        Requires branchId, companyId, productIds and deliveryType. Returns
        `{"items": [{"productId", "replacements": [...]}]}`.
        """
        ...

    async def get_promotions(self, context: SearchContext) -> dict[str, Any]:
        """Active promotions. Returns codes and titles only — no discount amounts;
        pass a `promotionCode` to `get_products` to see what is in one."""
        ...

    async def get_my_coupons(self) -> dict[str, Any]:
        """The user's coupons: `{"success", "summary", "coupons": [...]}`.

        Each carries `description`, `limitText`, `warningText` and `image` — and the
        schema is `additionalProperties: false`, so a discount **value** can never
        appear here. Nor can an eligible-product list, so coupons cannot be matched to
        cart lines. `get_coupon_details` is the only source of a number.
        """
        ...

    async def get_coupon_details(self, business_coupon_id: int) -> dict[str, Any]:
        """One coupon, with the `rewardText`/`rewardValue` the list endpoint omits.

        The parameter is `businessCouponId` — not `couponId`, and a **number**.
        """
        ...

    async def get_categories(self, context: SearchContext, **filters: Any) -> dict[str, Any]:
        """Category tree. Requires `branchId`, so it takes the context like the rest."""
        ...

    async def get_my_food_restrictions(self) -> dict[str, Any]: ...

    async def get_time_slots(
        self,
        *,
        branch_id: str,
        delivery_type: str,
        start: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Delivery slots: `{"success", "summary", "slots": [...], "meta"}`.

        `start` is not optional in practice. Without it Silpo returns a window
        beginning at the start of the current day, so by evening every slot in it has
        passed and `available` is false throughout — which reads exactly like "this
        branch has no slots". It must be a **timezone-qualified** ISO datetime;
        a date alone or a naive one returns HTTP 500.

        Only `available: true` slots can be chosen — Silpo's own description says so,
        and the list includes past slots.
        """
        ...

    # --- Cart. Reachable only through the deterministic pipeline, never the LLM. ---
    async def get_my_shopping_cart(self) -> dict[str, Any]:
        """`{"success", "shoppingCartId"}`. Silpo's docs call this "always the first
        step" — the cart carries the branch and delivery context search needs."""
        ...

    async def get_shopping_cart_by_id(self, shopping_cart_id: str) -> dict[str, Any]: ...

    async def add_or_update_cart_products(
        self, shopping_cart_id: str, products: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        """Appends; existing lines are untouched. Quantity is **set**, not incremented,
        which makes a retried sync idempotent (confirmed live — spec §3.1)."""
        ...

    async def remove_cart_products(
        self, shopping_cart_id: str, products: Sequence[dict[str, Any]]
    ) -> dict[str, Any]: ...

    # --- Introspection, for building tool declarations. ---
    async def list_tools(self) -> list[dict[str, Any]]: ...
