"""The two representations every shopping intent flows through.

Every intent handler produces a `DraftBasket` — what the user *wants*, as descriptions
and quantities, with no SKUs. The deterministic passes turn that into a `ResolvedCart`
of real Silpo products. Because every intent converges here, each one inherits
restriction filtering, substitution and coupon optimisation for free.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

ReasonKind = Literal["stated", "habit", "deal", "meal", "sub"]
"""Why a line is in the basket. Surfaced to the user, so it is never optional."""

BasketStatus = Literal["draft", "confirmed", "synced", "discarded"]


class DraftLine(BaseModel):
    description: str
    """What to buy, in the user's terms — "молоко 2,6% ~1 л". Not a SKU."""
    quantity: float = 1
    optional: bool = False
    """Trimmed first when a cart exceeds its budget cap."""
    reason_kind: ReasonKind = "stated"
    reason_text: str
    """Shown verbatim under the product name. Ukrainian prose, never a code."""


class DraftBasket(BaseModel):
    title: str
    intent: str
    lines: list[DraftLine]


class ResolvedLine(BaseModel):
    product_id: str
    company_id: str
    branch_id: str
    """Silpo needs all three to identify a product in a cart."""
    name: str
    qty: float
    unit: str
    unit_price: Decimal
    reason_kind: ReasonKind
    reason_text: str
    substituted_from: str | None = None
    """Original product name when the pass swapped an out-of-stock item."""
    optional: bool = False
    unavailable: bool = False
    """Kept visible so the user sees what is missing, but excluded from totals and sync."""

    @property
    def line_total(self) -> Decimal:
        """Price for this line.

        `qty` is a measurement (1.2 kg) and `unit_price` is money, so multiplying them
        directly is a TypeError. Converting through `str` keeps the float's binary
        representation out of a currency amount. This is the only place the two mix —
        call sites must use this rather than converting ad hoc.
        """
        return self.unit_price * Decimal(str(self.qty))


class ResolvedCart(BaseModel):
    lines: list[ResolvedLine]
    total: Decimal = Decimal("0")
    estimated_savings: Decimal = Decimal("0")
    """An estimate: Silpo applies coupons at checkout, not through the MCP."""
    savings_notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    """Degraded-mode labels, e.g. "degraded:coupons". Surfaced, never swallowed."""


class SyncReport(BaseModel):
    ok: bool
    """False if *any* line failed. A partial sync is never reported as success."""
    added: list[str] = Field(default_factory=list)
    failed: list[tuple[str, str]] = Field(default_factory=list)
    """(product name, error) for each line Silpo rejected."""
    checkout_web_link: str | None = None
