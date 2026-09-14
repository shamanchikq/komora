"""From Silpo's two history payloads to purchase events.

Every rule here was read off a real history on 2026-09-13 (reference §9, "Task 0
answers"), not off the tool descriptions:

* An online order counts only when `status == "received"`. `deliveredAt` is set on
  canceled orders too, so it proves nothing.
* An online line with `removed: true` sat in the order without being bought.
* A receipt lists a product more than once, and a till correction is a **negative**
  line: quantities are netted per product, and a net of zero or less was not bought.
* A receipt's `createdAt` carries no offset. It is a Silpo till, so it is Kyiv time.
* Weighted lines say `unit: "кг"` with a fractional quantity. Piece lines carry their
  pack size as `unit` («400г») — kept, because nothing else tells two sizes apart.
* Carrier bags are products whose **first word** is «Пакет». The substring rule threw
  out cottage cheese sold in a bag.
* Online `price` is hryvnias. `subtotal == price × quantity` held on 27 lines of 28.

Nothing personal survives: not the address, the shop, the city or the receipt URL.
The receipt is identified by a hash of its URL's token (21 distinct of 21), with
`(filId, createdAt)` — also 21 of 21 — as the fallback when the URL is missing.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
"""Where every Silpo till stands. Receipt timestamps have no offset of their own."""

Source = Literal["online", "offline"]

DELIVERED = "received"
BAG_WORDS = frozenset({"пакет", "пакунок"})
LAGER_PREFIX = "lager:"
"""Key prefix for a receipt line with no catalog product: countable, never a cart line."""


@dataclass(frozen=True)
class PurchaseEvent:
    """One product on one receipt or order, after netting."""

    source: Source
    receipt_key: str
    product_key: str
    name: str
    qty: float
    unit: str
    unit_price: Decimal
    weighted: bool
    reorderable: bool
    """The key is a catalog id, so the product can become a cart line."""
    bought_at: datetime
    """Aware, UTC. Delivery time for an order, till time for a receipt."""
    external_product_id: int | None = None


def first_word(name: Any) -> str:
    text = str(name or "").strip().lower()
    return text.replace("-", " ").split()[0] if text else ""


def is_bag(name: Any) -> bool:
    return first_word(name) in BAG_WORDS


def parse_time(stamp: Any) -> datetime | None:
    """An ISO timestamp as aware UTC; a naive one is read as Kyiv time."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KYIV)
    return parsed.astimezone(UTC)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"))
    except ArithmeticError, ValueError:
        return Decimal("0")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except TypeError, ValueError:
        return None


def online_purchases(payload: dict[str, Any]) -> list[PurchaseEvent]:
    """Delivered orders' lines, removed ones dropped, one event per (order, product).

    An order cannot list a product twice in the capture, but one that did would be
    summed here rather than overwritten — the same rule receipts already need.
    """
    events: list[PurchaseEvent] = []
    for order in payload.get("orders") or []:
        if not isinstance(order, dict) or order.get("status") != DELIVERED:
            continue
        delivered = parse_time((order.get("delivery") or {}).get("deliveredAt"))
        if delivered is None:
            continue
        order_key = str(order.get("orderId") or order.get("number") or "")
        if not order_key:
            continue
        per_product: dict[str, dict[str, Any]] = {}
        for line in order.get("products") or []:
            if not isinstance(line, dict) or line.get("removed") or is_bag(line.get("name")):
                continue
            key = str(line.get("id") or "")
            if not key:
                continue
            qty = float(line.get("quantity") or 0)
            if key in per_product:
                per_product[key]["qty"] += qty
                continue
            per_product[key] = {"line": line, "qty": qty}
        for key, entry in per_product.items():
            line, qty = entry["line"], entry["qty"]
            if qty <= 0:
                continue
            # An online line carries no `weighted` and no unit. A fractional quantity
            # is the only tell (the capture's «Свинина, ошийок» at 3.09), and a whole
            # number of kilograms reads as pieces — the safe direction: it is never
            # scaled by a pack size it does not have.
            weighted = not float(qty).is_integer()
            events.append(
                PurchaseEvent(
                    source="online",
                    receipt_key=f"o:{order_key}",
                    product_key=key,
                    name=str(line.get("name") or ""),
                    qty=qty,
                    unit="кг" if weighted else "",
                    unit_price=_decimal(line.get("price")),
                    weighted=weighted,
                    reorderable=True,
                    bought_at=delivered,
                )
            )
    return events


def receipt_key(receipt: dict[str, Any]) -> str | None:
    """A stable, non-identifying id for a receipt.

    The URL is an opaque token path — hashed, because the URL itself opens the
    receipt to anyone holding it. `(filId, createdAt)` is the fallback the capture
    found equally unique.
    """
    url = str(receipt.get("receiptUrl") or "").strip()
    if url:
        token = url.rstrip("/").rsplit("/", 1)[-1]
        return "r:" + hashlib.sha256(token.encode()).hexdigest()[:32]
    fil, created = receipt.get("filId"), receipt.get("createdAt")
    if fil is not None and created:
        return f"f:{fil}:{created}"
    return None


def offline_purchases(payload: dict[str, Any]) -> list[PurchaseEvent]:
    """Receipt lines netted per product; a product netting to zero or less was returned."""
    events: list[PurchaseEvent] = []
    for receipt in payload.get("orders") or []:
        if not isinstance(receipt, dict):
            continue
        key = receipt_key(receipt)
        bought = parse_time(receipt.get("createdAt"))
        if key is None or bought is None:
            continue
        net: dict[str, float] = defaultdict(float)
        first: dict[str, dict[str, Any]] = {}
        for line in receipt.get("products") or []:
            if not isinstance(line, dict) or is_bag(line.get("name")):
                continue
            catalog = line.get("catalogProduct")
            product_key = (
                str(catalog["id"])
                if isinstance(catalog, dict) and catalog.get("id")
                else f"{LAGER_PREFIX}{line.get('lagerId')}"
            )
            net[product_key] += float(line.get("quantity") or 0)
            first.setdefault(product_key, line)
        for product_key, qty in net.items():
            if qty <= 0:
                continue
            line = first[product_key]
            raw_catalog = line.get("catalogProduct")
            known: dict[str, Any] = raw_catalog if isinstance(raw_catalog, dict) else {}
            unit = str(line.get("unit") or "")
            weighted = bool(known.get("weighted")) or unit.lower() == "кг"
            events.append(
                PurchaseEvent(
                    source="offline",
                    receipt_key=key,
                    product_key=product_key,
                    name=str(line.get("name") or known.get("name") or ""),
                    qty=round(qty, 3),
                    unit=unit,
                    unit_price=_decimal(line.get("price")),
                    weighted=weighted,
                    reorderable=not product_key.startswith(LAGER_PREFIX),
                    bought_at=bought,
                    external_product_id=_int_or_none(line.get("lagerId")),
                )
            )
    return events
