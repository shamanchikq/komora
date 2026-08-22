"""The two representations every shopping intent flows through.

Every intent handler produces a `DraftBasket` — what the user *wants*, as descriptions
and quantities, with no SKUs. The deterministic passes turn that into a `ResolvedCart`
of real Silpo products. Because every intent converges here, each one inherits
restriction filtering, substitution and coupon optimisation for free.
"""

import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, computed_field, field_validator

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
    category: Prose | None = None
    """Silpo category title, when the model can name one — «Курячі яйця».

    Free-text search cannot tell hen's eggs from guinea fowl's; Silpo's own taxonomy
    keeps them in separate categories. Matched to a slug in `passes/categories.py`,
    and ignored when it matches nothing.
    """
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
    removals: list[Prose] = Field(default_factory=list)
    """What the user asked to take back out, in their own words — «ковбаски пепероні».

    A basket already sent to Silpo cannot be edited by proposing a new one: appending
    a replacement leaves the original sitting in the cart, which is exactly what the
    user asked to be rid of. These are *descriptions*, never SKUs — the model names
    what to drop and `passes/removals.py` decides whether anything Komora added
    actually matches.
    """
    warnings: list[str] = Field(default_factory=list)
    """Carried forward into the ResolvedCart — e.g. a line dropped by a restriction."""

    _clean = field_validator("title")(clean_prose)

    @field_validator("removals")
    @classmethod
    def _clean_removals(cls, value: list[str]) -> list[str]:
        return [cleaned for cleaned in (clean_prose(item) for item in value) if cleaned]


class CartRemoval(BaseModel):
    """One product to take out of the user's real Silpo cart.

    Only ever built from a line Komora itself synced. Nothing the user put in their
    cart by hand is a candidate: Komora is a guest there, and a grocery agent that
    deletes things it did not add is not one anybody should run.
    """

    product_id: str
    name: str
    """Shown on the confirmation sheet — a removal has to be recognisable to approve."""


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
    description: str = ""
    """What the model asked for, kept alongside what was chosen.

    Two things need it after resolution: the verification pass, which compares the two,
    and «інший варіант», which re-runs the same query to offer the next candidate.
    """
    category: str | None = None
    """The Silpo category this line was resolved from, when one matched.

    Kept so «інший варіант» offers alternatives from the same shelf. Without it a swap
    falls back to free-text search and can walk off into a different category — the
    exact confusion the category lookup existed to prevent.
    """
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
    weighted: bool = False
    """Priced and ordered per kilogram: `unit_price` is then ₴/kg and `qty` is kg.
    A search hit carries no size field at all, so this is also the only signal a
    surface has for showing «0,15 кг × 999,00 ₴/кг» instead of a piece count."""
    step: float | None = None
    """The smallest orderable weight of a weighted good (0,1 cheese, 0,25 bacon).
    An unqualified request resolves to exactly one of these — never one kilo."""
    stock: float | None = None
    """Silpo's remaining stock when the search returned it. The ceiling a quantity
    control must respect; `None` means unknown, not unlimited."""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def line_total(self) -> Decimal:
        """Price for this line, and part of every serialisation (`model_dump`) so a
        second surface never multiplies money itself.

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
    """Per-line discounts. Owned by `apply_savings` and regenerated whenever the lines
    change."""
    coupon_notes: list[str] = Field(default_factory=list)
    """The user's coupons, which have nothing to do with which products are in the cart.

    Separate from `savings_notes` because the two have different lifetimes: swapping a
    product must recompute the discounts and leave the coupons alone. Sharing one list
    meant a swap silently dropped «-10% на онлайн чек».
    """
    removals: list[CartRemoval] = Field(default_factory=list)
    """Products to take out of the Silpo cart when this basket is confirmed.

    Deliberately not `lines`: they are not part of the basket's total, they are not
    rendered as products, and they need their own line on the confirmation sheet. The
    user is agreeing to two different things, so they are shown as two different things.
    """
    warnings: list[str] = Field(default_factory=list)
    """Degraded-mode labels, e.g. "degraded:coupons". Surfaced, never swallowed."""


class SyncReport(BaseModel):
    ok: bool
    """False if *any* line failed. A partial sync is never reported as success."""
    added: list[str] = Field(default_factory=list)
    failed: list[tuple[str, str]] = Field(default_factory=list)
    """(product name, error) for each line Silpo rejected."""
    removed: list[str] = Field(default_factory=list)
    remove_failed: list[tuple[str, str]] = Field(default_factory=list)
    """A removal that did not happen counts against `ok` exactly like a failed add:
    the user asked for the cart to end up a certain way and it did not."""
    checkout_web_link: str | None = None
    checkout_mobile_link: str | None = None
    """Silpo's tool descriptions ask for both — «Оформити на сайті» and «в застосунку».
    Present only once the cart is checkout-ready."""
    blocking_validations: list[str] = Field(default_factory=list)
    """Error codes from `calculation.validations[]` — why there may be no link."""
