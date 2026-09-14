"""The recurring loop (J1): keep history current, then speak — rarely, and only when due.

One asyncio task beside the poller and the HTTP server, in the same process, on
purpose: `AuthorizationBridge` and long polling are both single-process, and a worker
of its own would need the bot handle this process already holds.

Each tick, for every linked user:

1. **Refresh**, if the last successful read is older than `REFRESH_EVERY`, through
   `connect_background` — a session that yields to a user's turn (`Busy`) rather than
   queueing behind it. Receipts need the cart's context and are skipped without it;
   the skip is recorded (`history_imports`), not hidden.
2. **Nudge**, if a habit is due, nudgeable and not muted, outside quiet hours and
   outside the per-product cooldown. One message, offering a draft. Never a cart write.
3. **Digest**, on a Sunday evening, once a week, to a user who asked for it
   (`/digest on`) — from stored receipts and purchases, so it needs no cart context
   (Plan 4 Task 4). The deal scan is *not* here: it needs a live cart context, which
   a tick rarely holds, so it runs after a turn (`handlers._schedule_prices`).

A failure on one user is logged and never stops the others; a failure on one source
never blocks the other. Nothing here makes a model request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from komora.bot.handlers import Services, nudge_for, refresh_history, send_digest_if_due
from komora.core.mcp.errors import McpError, NotAuthenticated
from komora.core.mcp.gateway import Busy

log = logging.getLogger(__name__)

TICK_EVERY = timedelta(hours=1)
REFRESH_EVERY = timedelta(hours=12)
"""Online orders and receipts change a few times a week; twice a day is plenty and
keeps the calls per user per day in single digits."""


async def refresh_if_stale(services: Services, telegram_id: int, now: datetime) -> bool:
    """One user's refresh. Returns whether history was read."""
    stores = services.habits
    if stores is None:
        return False
    freshest = [
        t
        for t in (
            await stores.imports.last_ok(telegram_id, "online"),
            await stores.imports.last_ok(telegram_id, "offline"),
        )
        if t is not None
    ]
    if freshest and now - max(freshest) < REFRESH_EVERY:
        return False
    # A failure waits a full interval too. A user whose tokens stopped working is still
    # "linked" — the row holds tokens — so the job tried every hour and wrote a `failed`
    # row each time, for ever. Twice a day is what a healthy user costs; a broken one
    # should cost no more.
    failed = await stores.imports.last_failed(telegram_id, "online")
    if failed is not None and now - failed < REFRESH_EVERY:
        return False

    connect = stores.connect_background or services.connect
    try:
        async with connect(telegram_id) as mcp:
            await refresh_history(services, telegram_id, mcp, now=now)
            return True
    except Busy:
        await stores.imports.record(
            telegram_id, "online", "skipped", "a turn was in flight", at=now
        )
        return False
    except NotAuthenticated:
        # Tokens are gone or unusable; nothing to read until the user links again.
        await stores.imports.record(telegram_id, "online", "failed", "not authenticated", at=now)
        return False
    except McpError as exc:
        await stores.imports.record(
            telegram_id, "online", "failed", f"{type(exc).__name__}: {exc}", at=now
        )
        return False


async def tick(services: Services, *, now: datetime | None = None) -> list[int]:
    """One pass over every linked user. Returns who was nudged."""
    now = now or datetime.now(UTC)
    nudged: list[int] = []
    if services.habits is None:
        return nudged
    for telegram_id in await services.users.linked():
        try:
            await refresh_if_stale(services, telegram_id, now)
            nudge = await nudge_for(services, telegram_id, now=now)
            if nudge is not None:
                await services.notify(telegram_id, nudge)
                nudged.append(telegram_id)
            # Stored data only; the coupon section would need a session and is left
            # out here rather than opening one per subscriber per tick.
            await send_digest_if_due(services, telegram_id, now=now)
        except Exception:
            # One user's failure is that user's; the loop goes on.
            log.exception("habits tick failed for %s", telegram_id)
    return nudged


async def run_habits_job(
    services: Services,
    *,
    every: timedelta = TICK_EVERY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Loop forever. The first tick waits a full interval, so a restart does not fire
    a burst of Silpo calls before the bot has even answered its first message."""
    while True:
        await sleep(every.total_seconds())
        try:
            await tick(services)
        except Exception:
            log.exception("habits tick crashed")
