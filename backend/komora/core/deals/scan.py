"""The deal scan: tracked products re-priced by article, and the branch's discounts ranked.

Three rules from Plan 4, each measured before it was written (reference §10.4):

* **A deal is `oldPrice > price` on a product id.** Never a coupon, never a promotion
  title. The tracked habits carry their article number, `find_products_batch` takes
  up to thirty articles per call, and every hit carries both prices — so "your usual
  cheese is 33 % off" is an intersection of ids, one call, exact.
* **A miss is unknown, not «no deal».** The tool's description says an out-of-stock
  product may be missing from the result entirely (not observed). A product that did
  not come back is reported as unknown and no snapshot is written for it.
* **List order is never a ranking.** `sortBy: price` orders on `displayPrice`, which
  mixes per-100 g prices with per-piece ones. A top-ten by discount is computed here.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from statistics import median
from typing import Any

from komora.core.deals.models import Snapshot
from komora.core.habits.engine import Habit
from komora.core.mcp.protocol import SilpoClient
from komora.core.models import SearchContext
from komora.core.passes.resolve import (
    MAX_QUERIES_PER_BATCH,
    flatten_search,
    in_stock,
    usable,
)


@dataclass(frozen=True)
class ScanResult:
    snapshots: list[Snapshot]
    unknown: list[str]
    """Product keys the search returned nothing for — a miss, not a verdict."""


def _term_for(habit: Habit) -> str:
    return str(habit.external_product_id) if habit.external_product_id is not None else habit.name


def _matches(habit: Habit, hit: dict[str, Any]) -> bool:
    """By article when one is stored, by the stored id otherwise — never by name."""
    if habit.external_product_id is not None:
        return str(hit.get("externalProductId")) == str(habit.external_product_id)
    return str(hit.get("id")) == habit.product_key


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def snapshot_of(
    habit: Habit, hit: dict[str, Any], *, branch_id: str, today: date, now: datetime
) -> Snapshot:
    price = _decimal(hit.get("price")) or Decimal("0")
    return Snapshot(
        product_key=habit.product_key,
        external_product_id=habit.external_product_id,
        branch_id=branch_id,
        day=today,
        price=price,
        old_price=_decimal(hit.get("oldPrice")),
        display_price=_decimal(hit.get("displayPrice")),
        available=in_stock(hit),
        captured_at=now,
    )


async def snapshot_tracked(
    mcp: SilpoClient,
    context: SearchContext,
    habits: Sequence[Habit],
    *,
    today: date,
    now: datetime,
) -> ScanResult:
    """One shelf price per tracked, reorderable habit — ≤ 30 products per call."""
    tracked = [h for h in habits if h.reorderable]
    terms = list(dict.fromkeys(_term_for(h) for h in tracked))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(terms), MAX_QUERIES_PER_BATCH):
        chunk = terms[start : start + MAX_QUERIES_PER_BATCH]
        grouped |= flatten_search(await mcp.find_products_batch(chunk, context))

    snapshots: list[Snapshot] = []
    unknown: list[str] = []
    for habit in tracked:
        hits = [p for p in grouped.get(_term_for(habit), []) if usable(p)]
        hit = next((p for p in hits if _matches(habit, p)), None)
        if hit is None:
            unknown.append(habit.product_key)
            continue
        snapshots.append(snapshot_of(habit, hit, branch_id=context.branch_id, today=today, now=now))
    return ScanResult(snapshots=snapshots, unknown=unknown)


def deals_among(snapshots: Sequence[Snapshot]) -> list[Snapshot]:
    """The tracked products on promotion right now, deepest discount first."""
    return sorted(
        (s for s in snapshots if s.discounted and s.available),
        key=lambda s: (-s.percent_off, s.product_key),
    )


USUAL_MIN_SNAPSHOTS = 4
"""How many distinct days of shelf prices a «звичайна ціна» needs (Plan 4 D3). Fewer,
and the only honest sentence is the exact one Silpo already gives — «замість»."""
USUAL_WINDOW_DAYS = 60


def usual_price(history: Sequence[Snapshot]) -> Decimal | None:
    """The median shelf price over the window — `None` below the threshold.

    Shelf prices only: `purchases.unit_price` is what was *paid*, after a discount, and
    a baseline that mixed the two would call an ordinary week a price drop.
    """
    by_day: dict[date, Decimal] = {}
    for snapshot in history:
        if snapshot.price > 0:
            by_day[snapshot.day] = snapshot.price
    if len(by_day) < USUAL_MIN_SNAPSHOTS:
        return None
    return Decimal(str(median(by_day.values()))).quantize(Decimal("0.01"))


def percent_below(price: Decimal, usual: Decimal) -> int | None:
    """Whole percent below the usual price, or `None` when not below it."""
    if usual <= 0 or price >= usual:
        return None
    return int((usual - price) * 100 / usual)


@dataclass(frozen=True)
class BranchDeal:
    """One discounted product from a branch-wide browse, for the deals screen."""

    product_id: str
    name: str
    price: Decimal
    old_price: Decimal
    percent_off: int
    weighted: bool
    display_ratio: str | None
    external_product_id: int | None
    company_id: str
    branch_id: str


MAX_BRANCH_DEALS = 10


def rank_branch_deals(
    products: Sequence[dict[str, Any]], limit: int = MAX_BRANCH_DEALS
) -> list[BranchDeal]:
    """Deepest discount first, computed — never Silpo's list order."""
    deals: list[BranchDeal] = []
    for product in products:
        if not usable(product) or not in_stock(product):
            continue
        price, old = _decimal(product.get("price")), _decimal(product.get("oldPrice"))
        if price is None or old is None or old <= price or old <= 0:
            continue
        article = product.get("externalProductId")
        ratio = product.get("displayRatio")
        deals.append(
            BranchDeal(
                product_id=str(product["id"]),
                name=str(product.get("name") or ""),
                price=price,
                old_price=old,
                percent_off=int((old - price) * 100 / old),
                weighted=bool(product.get("weighted")),
                display_ratio=str(ratio) if isinstance(ratio, str) else None,
                external_product_id=int(article) if isinstance(article, int) else None,
                company_id=str(product.get("companyId") or ""),
                branch_id=str(product.get("branchId") or ""),
            )
        )
    deals.sort(key=lambda d: (-d.percent_off, d.name))
    return deals[:limit]


def promo_texts(payload: Any) -> list[str]:
    """Personal offers as one line each — «x25 балобонусів за свіжу свинину».

    Prose only: no write tool activates one, so Komora can only point at the Silpo
    app, and it says so where these are shown.
    """
    if not isinstance(payload, dict):
        return []
    out: list[str] = []
    for promo in payload.get("promos") or []:
        if not isinstance(promo, dict):
            continue
        reward = " ".join(str(promo.get("rewardText") or "").split())
        condition = " ".join(str(promo.get("description") or "").split())
        text = " — ".join(part for part in (reward, condition) if part)
        if text:
            out.append(text + (" ✓" if promo.get("selected") else ""))
    return out
