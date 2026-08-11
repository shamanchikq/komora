"""A Silpo stand-in built from the real captured response shapes.

Product dicts mirror `tests/fixtures/mcp/find_products_batch.json` exactly — `id` (not
`productId`), `stock`, `available`, `step`, `oldPrice` — so a pass that works here
works against the live server.
"""

from collections.abc import Sequence
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


class FakeSilpo:
    """Records calls so tests can assert batching and arguments, not just results."""

    def __init__(
        self,
        results: dict[str, list[dict[str, Any]]] | None = None,
        replacements: dict[str, list[dict[str, Any]]] | None = None,
        *,
        replacements_fail: bool = False,
    ) -> None:
        self._results = results or {}
        self._replacements = replacements or {}
        self._replacements_fail = replacements_fail
        self.search_calls: list[list[str]] = []
        self.replacement_calls: list[list[str]] = []

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
