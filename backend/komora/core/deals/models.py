"""The shapes the deals code and the repositories share."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class Snapshot:
    """One product's shelf price on one day at one branch (`price_snapshots`)."""

    product_key: str
    external_product_id: int | None
    branch_id: str
    day: date
    price: Decimal
    old_price: Decimal | None
    display_price: Decimal | None
    available: bool
    captured_at: datetime

    @property
    def discounted(self) -> bool:
        return self.old_price is not None and self.old_price > self.price

    @property
    def percent_off(self) -> int:
        """`(old − price) / old`, whole percent. Zero when not discounted."""
        if self.old_price is None or self.old_price <= 0 or not self.discounted:
            return 0
        return int((self.old_price - self.price) * 100 / self.old_price)
