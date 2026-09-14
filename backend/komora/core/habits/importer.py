"""Reading a user's history from Silpo, page by page, into purchase events.

Two sources with different costs (reference §9):

* **Online orders** need no context and page fifty at a time, newest first. Only four
  of ninety-seven orders in the one history seen carried lines — older ones are
  headers — so the walk stops at the first order older than the last import, and a
  backfill reads everything.
* **Receipts** need the cart's branch, delivery type and timeslot, page ten at a time,
  and take `dateStart`/`dateEnd`, which default to a six-month window on the server. A
  refresh sends `dateStart` = the last successful import minus a day; a backfill sends
  two years, which is also how far back receipts have ever been asked for.

No context means receipts are **skipped**, and the skip is reported, not hidden: a
`ImportReport` says what each source did.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from komora.core.habits.purchases import (
    KYIV,
    PurchaseEvent,
    offline_purchases,
    online_purchases,
    parse_time,
)
from komora.core.models import SearchContext

ONLINE_PAGE = 50
OFFLINE_PAGE = 10
MAX_PAGES = 40
"""A ceiling on paging, so a `meta.total` that lies cannot spin forever."""

BACKFILL = timedelta(days=730)
REFRESH_MARGIN = timedelta(days=1)


class HistoryReader(Protocol):
    async def get_my_online_orders(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]: ...

    async def get_my_offline_orders(
        self,
        context: SearchContext,
        *,
        limit: int = 10,
        offset: int = 0,
        date_start: str | None = None,
        date_end: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ImportReport:
    online: int | None = None
    """Events read from online orders; `None` when the source failed."""
    offline: int | None = None
    """Events read from receipts; `None` when skipped or failed."""
    skipped: str | None = None
    """Why receipts were not read — no cart context, most often."""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _orders(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    return [o for o in payload.get("orders") or [] if isinstance(o, dict)]


def _total(payload: Any, fallback: int) -> int:
    meta = payload.get("meta") if isinstance(payload, dict) else None
    total: Any = (meta or {}).get("total")
    try:
        return int(total)
    except TypeError, ValueError:
        return fallback


def date_start_for(since: datetime | None, now: datetime) -> str:
    """`dateStart` as the tool wants it: naive ISO, Kyiv, midnight.

    `since` is the last successful import; a backfill has none and reaches two years.
    """
    start = (since - REFRESH_MARGIN) if since is not None else (now - BACKFILL)
    return start.astimezone(KYIV).strftime("%Y-%m-%dT00:00:00")


async def read_online(mcp: HistoryReader, *, stop_before: datetime | None) -> list[PurchaseEvent]:
    """Every delivered order's lines, newest first, until one predates `stop_before`."""
    events: list[PurchaseEvent] = []
    offset = 0
    for _ in range(MAX_PAGES):
        payload = await mcp.get_my_online_orders(limit=ONLINE_PAGE, offset=offset)
        orders = _orders(payload)
        if not orders:
            break
        events.extend(online_purchases({"orders": orders}))
        oldest = min(
            (t for t in (parse_time(o.get("createdAt")) for o in orders) if t is not None),
            default=None,
        )
        offset += len(orders)
        if offset >= _total(payload, offset) or (
            stop_before is not None and oldest is not None and oldest < stop_before
        ):
            break
    return events


async def read_offline(
    mcp: HistoryReader, context: SearchContext, *, since: datetime | None, now: datetime
) -> list[PurchaseEvent]:
    """Receipts from `since` (or the backfill horizon) to now, ten a page."""
    events: list[PurchaseEvent] = []
    offset = 0
    start = date_start_for(since, now)
    for _ in range(MAX_PAGES):
        payload = await mcp.get_my_offline_orders(
            context, limit=OFFLINE_PAGE, offset=offset, date_start=start
        )
        orders = _orders(payload)
        if not orders:
            break
        events.extend(offline_purchases({"orders": orders}))
        offset += len(orders)
        if offset >= _total(payload, offset):
            break
    return events


class PurchaseSink(Protocol):
    async def upsert(self, user_id: int, events: Sequence[PurchaseEvent]) -> int: ...


async def import_history(
    mcp: HistoryReader,
    sink: PurchaseSink,
    user_id: int,
    *,
    context: SearchContext | None,
    since_online: datetime | None,
    since_offline: datetime | None,
    now: datetime | None = None,
    include_online: bool = True,
) -> ImportReport:
    """Read both sources into `sink`. A failed source is an error, a missing context
    is a skip, and neither stops the other source.

    `include_online=False` reads receipts alone — what a turn that already holds a
    fresh cart context does after its reply; online orders need no context and are
    kept current by the job."""
    now = now or datetime.now(UTC)
    online: int | None = None
    offline: int | None = None
    skipped: str | None = None
    errors: list[str] = []

    if include_online:
        try:
            online = await sink.upsert(user_id, await read_online(mcp, stop_before=since_online))
        except Exception as exc:
            errors.append(f"online: {type(exc).__name__}: {exc}")

    if context is None:
        skipped = "receipts need a cart with a branch and a timeslot"
    else:
        try:
            offline = await sink.upsert(
                user_id, await read_offline(mcp, context, since=since_offline, now=now)
            )
        except Exception as exc:
            errors.append(f"offline: {type(exc).__name__}: {exc}")

    return ImportReport(online=online, offline=offline, skipped=skipped, errors=errors)
