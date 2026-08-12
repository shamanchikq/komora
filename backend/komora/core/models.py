"""The two representations every shopping intent flows through.

Every intent handler produces a `DraftBasket` — what the user *wants*, as descriptions
and quantities, with no SKUs. The deterministic passes turn that into a `ResolvedCart`
of real Silpo products. Because every intent converges here, each one inherits
restriction filtering, substitution and coupon optimisation for free.
"""

import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

ReasonKind = Literal["stated", "habit", "deal", "meal", "sub"]
"""Why a line is in the basket. Surfaced to the user, so it is never optional."""

BasketStatus = Literal["draft", "confirmed", "synced", "discarded"]

_MARKUP = re.compile(r"<[^>]*>")
MAX_PROSE = 200
"""Long enough for any honest description; short enough to keep one line one line."""


def clean_prose(value: str) -> str:
    """Strip stray markup and collapse whitespace in text the model wrote.

    Observed live: gemma4:12b titled a basket «Базові продукти</div>». Escaping kept
    the message from breaking, so the user simply read a stray `</div>` — which looks
    like a bug in Komora rather than a slip by the model. Every string here is shown
    to a person, so it is cleaned at the boundary rather than at each render site.

    Over-long text is truncated, never rejected: losing a whole basket because the
    model was verbose would be a worse outcome than a clipped title.
    """
    cleaned = " ".join(_MARKUP.sub(" ", value).split())
    return cleaned if len(cleaned) <= MAX_PROSE else cleaned[: MAX_PROSE - 1].rstrip() + "…"


Prose = Annotated[str, StringConstraints(strip_whitespace=True)]


class DraftLine(BaseModel):
    description: Prose
    """What to buy, in the user's terms — "молоко 2,6% ~1 л". Not a SKU."""
    quantity: float = 1
    optional: bool = False
    """Trimmed first when a cart exceeds its budget cap."""
    reason_kind: ReasonKind = "stated"
    reason_text: Prose
    """Shown verbatim under the product name. Ukrainian prose, never a code."""

    _clean = field_validator("description", "reason_text")(clean_prose)


class DraftBasket(BaseModel):
    title: Prose
    """Shown as the heading and stored as the basket's name."""
    intent: str
    lines: list[DraftLine]
    warnings: list[str] = Field(default_factory=list)
    """Carried forward into the ResolvedCart — e.g. a line dropped by a restriction."""

    _clean = field_validator("title")(clean_prose)


class SearchContext(BaseModel):
    """Branch and delivery context, without which Silpo will not search.

    `find_products_batch` requires all four, and they come from the user's cart:
    `cart.shipments[0].branchId`, `cart.deliveryType`, `cart.timeslot.start/.end`.
    Confirmed live 2026-08-11 — see spec §3.1.
    """

    branch_id: str
    delivery_type: str
    timeslot_start: str
    timeslot_end: str

    def as_tool_args(self) -> dict[str, str]:
        return {
            "branchId": self.branch_id,
            "deliveryType": self.delivery_type,
            "timeslotStart": self.timeslot_start,
            "timeslotEnd": self.timeslot_end,
        }


class ResolvedLine(BaseModel):
    product_id: str
    company_id: str
    branch_id: str
    """Silpo needs all three to identify a product in a cart."""
    name: str
    qty: float
    unit: str
    unit_price: Decimal
    old_price: Decimal | None = None
    """Silpo's pre-discount price when the product is on promotion. The discount is
    already applied to `unit_price`, so `old_price - unit_price` is the real saving —
    the only machine-readable discount data Silpo exposes."""
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
    checkout_mobile_link: str | None = None
    """Silpo's tool descriptions ask for both — «Оформити на сайті» and «в застосунку».
    Present only once the cart is checkout-ready."""
    blocking_validations: list[str] = Field(default_factory=list)
    """Error codes from `calculation.validations[]` — why there may be no link."""
