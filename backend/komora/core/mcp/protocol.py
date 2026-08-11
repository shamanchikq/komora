"""The Silpo surface the rest of Komora depends on.

Everything downstream — the passes, the agent loop, sync — depends on this Protocol
rather than the concrete client, so the whole pipeline is testable without a network
or an OAuth flow.

Payloads are loose `dict`s on purpose: the real response shapes are captured from the
live server in Task 7, and only fields verified there should be read.
"""

from typing import Any, Protocol


class SilpoClient(Protocol):
    # --- Reads. Safe for the agent to call freely. ---
    async def find_products_batch(self, queries: list[str]) -> list[dict[str, Any]]:
        """Search several products at once. Silpo accepts up to 30 per call."""
        ...

    async def get_products(self, **filters: Any) -> list[dict[str, Any]]: ...

    async def get_product_details(self, slug: str) -> dict[str, Any]: ...

    async def get_replacements(self, slug: str) -> list[dict[str, Any]]:
        """Substitutes for an out-of-stock product."""
        ...

    async def get_promotions(self, branch_id: str | None = None) -> list[dict[str, Any]]: ...

    async def get_my_coupons(self) -> list[dict[str, Any]]: ...

    async def get_categories(self) -> list[dict[str, Any]]: ...

    async def get_my_food_restrictions(self) -> list[dict[str, Any]]: ...

    # --- Cart. Reachable only through the deterministic pipeline, never the LLM. ---
    async def get_my_shopping_cart(self) -> str:
        """The active cart id. Silpo's docs call this 'always the first step'."""
        ...

    async def get_shopping_cart_by_id(self, cart_id: str) -> dict[str, Any]: ...

    async def add_or_update_cart_products(
        self, cart_id: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Upserts by product; appends to whatever the user already has."""
        ...

    async def remove_cart_products(
        self, cart_id: str, product_ids: list[str]
    ) -> dict[str, Any]: ...

    # --- Introspection, for capturing schemas and building tool declarations. ---
    async def list_tools(self) -> list[dict[str, Any]]: ...
