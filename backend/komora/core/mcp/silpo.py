"""The Silpo MCP surface, bound to one open session.

Every argument name here comes from `tests/fixtures/mcp/tools.json` — the schemas the
live server publishes — and not from the tool names. That distinction is the whole
reason this file exists: `shoppingCartId` not `cartId`, `products` not `queries`,
`productIds` for replacements but `products` for removal, `deliveryTypes` (plural, an
array) for timeslots but `deliveryType` everywhere else.

Cart payloads carry **only the four declared fields**. `name` and `price` are not in
the schema; the A1 probe that verified append semantics never sent them, so sending
them now would be an unverified change to the one call the product depends on.
"""

from collections.abc import Sequence
from typing import Any

from mcp import ClientSession

from komora.core.mcp.client import DEFAULT_RETRY_POLICY, RetryPolicy, call_tool, with_retry
from komora.core.mcp.errors import McpError
from komora.core.mcp.payload import as_dict, error_of, unwrap
from komora.core.models import SearchContext


class ToolFailed(McpError):
    """Silpo accepted the request and answered with a failure."""


class SilpoSession:
    """Implements `SilpoClient` over an initialised MCP session.

    One instance wraps one session, so it lives for the duration of a single user
    turn. Retries are per call: a rate limit or a 5xx is worth another attempt, a
    validation error never is.
    """

    def __init__(
        self, session: ClientSession, *, policy: RetryPolicy = DEFAULT_RETRY_POLICY
    ) -> None:
        self._session = session
        self._policy = policy

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        async def once() -> Any:
            return unwrap(await call_tool(self._session, tool, args))

        payload = await with_retry(once, policy=self._policy)
        problem = error_of(payload)
        if problem is not None:
            raise ToolFailed(f"{tool}: {problem}")
        return as_dict(payload)

    # --- Reads ---
    async def find_products_batch(
        self, queries: Sequence[str], context: SearchContext
    ) -> dict[str, Any]:
        return await self._call(
            "silpo_find_products_batch",
            {**context.as_tool_args(), "products": list(queries)},
        )

    async def get_products(self, context: SearchContext, **filters: Any) -> dict[str, Any]:
        return await self._call("silpo_get_products", {**context.as_tool_args(), **filters})

    async def get_product_details(self, slug: str, context: SearchContext) -> dict[str, Any]:
        return await self._call(
            "silpo_get_product_details", {**context.as_tool_args(), "slug": slug}
        )

    async def get_replacements(
        self, *, product_ids: Sequence[str], company_id: str, context: SearchContext
    ) -> dict[str, Any]:
        # Declares only these four — no timeslot, unlike every other search call.
        return await self._call(
            "silpo_get_replacements",
            {
                "branchId": context.branch_id,
                "deliveryType": context.delivery_type,
                "companyId": company_id,
                "productIds": list(product_ids),
            },
        )

    async def get_promotions(self, context: SearchContext) -> dict[str, Any]:
        return await self._call("silpo_get_promotions", context.as_tool_args())

    async def get_categories(self, context: SearchContext, **filters: Any) -> dict[str, Any]:
        # Needs only the branch, but takes the whole context so every context-bearing
        # call looks identical to the agent loop's dispatcher.
        return await self._call("silpo_get_categories", {"branchId": context.branch_id, **filters})

    async def get_my_coupons(self) -> dict[str, Any]:
        return await self._call("silpo_get_my_coupons", {})

    async def get_my_food_restrictions(self) -> dict[str, Any]:
        return await self._call("silpo_get_my_food_restrictions", {})

    async def get_time_slots(
        self, *, branch_id: str, delivery_type: str, limit: int = 10
    ) -> dict[str, Any]:
        return await self._call(
            "silpo_get_time_slots",
            {"branchId": branch_id, "deliveryTypes": [delivery_type], "limit": limit},
        )

    # --- Cart ---
    async def get_my_shopping_cart(self) -> dict[str, Any]:
        return await self._call("silpo_get_my_shopping_cart", {})

    async def get_shopping_cart_by_id(self, shopping_cart_id: str) -> dict[str, Any]:
        return await self._call(
            "silpo_get_shopping_cart_by_id", {"shoppingCartId": shopping_cart_id}
        )

    async def add_or_update_cart_products(
        self, shopping_cart_id: str, products: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        """Appends, and **sets** each quantity rather than adding to it.

        Silpo exposes `addQuantity: true` to sum instead. Komora leaves it off: the
        replacing behaviour is what the A1 probe verified, and it is what makes a
        retried sync idempotent.
        """
        return await self._call(
            "silpo_add_or_update_cart_products",
            {"shoppingCartId": shopping_cart_id, "products": [_cart_item(p) for p in products]},
        )

    async def remove_cart_products(
        self, shopping_cart_id: str, products: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self._call(
            "silpo_remove_cart_products",
            {"shoppingCartId": shopping_cart_id, "products": [_cart_item(p) for p in products]},
        )

    # --- Introspection ---
    async def list_tools(self) -> list[dict[str, Any]]:
        """Declarations for `build_tool_decls`. Attributes are snake_case in the SDK."""
        result = await self._session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in result.tools
        ]


_CART_ITEM_FIELDS = ("productId", "companyId", "branchId", "quantity")


def _cart_item(product: dict[str, Any]) -> dict[str, Any]:
    """Keep only what the schema declares.

    Silpo does not mark the object `additionalProperties: false`, but nothing verifies
    that its validator agrees, and this is the one call whose failure loses the user
    their basket.
    """
    missing = [field for field in _CART_ITEM_FIELDS if product.get(field) is None]
    if missing:
        raise ValueError(f"cart item is missing required fields {missing}: {product}")
    return {field: product[field] for field in _CART_ITEM_FIELDS}
