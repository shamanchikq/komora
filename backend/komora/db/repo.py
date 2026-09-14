"""Repositories — the only place SQL is written.

Each method opens its own session, so callers never manage transactions.
"""

import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from komora.core.habits.engine import Habit
from komora.core.habits.purchases import PurchaseEvent, Source
from komora.core.models import BasketStatus, CartRemoval, ResolvedCart, ResolvedLine
from komora.db.base import utcnow
from komora.db.tables import (
    ConversationMessage,
    DraftBasketRow,
    DraftItem,
    HabitMute,
    HistoryImport,
    Notification,
    OAuthClientRegistration,
    ProductHabit,
    Purchase,
    User,
)

_REGISTRATION_ID = 1

SYNCED_BASKETS = 5
"""How far back «прибери молоко» may reach.

Bounded because a removal candidate is a product Komora believes is still in the cart,
and that belief decays: the user checks out, empties the cart, or removes the item in
the Silpo app. A stale candidate is harmless — `sync` only removes what a fresh read
shows is actually there — but the list is shown on the confirmation sheet, and offering
to remove something bought last month would be nonsense.
"""


class UserRepo:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def ensure(self, telegram_id: int) -> User:
        async with self._sessions() as session, session.begin():
            user: User | None = await session.get(User, telegram_id)
            if user is None:
                user = User(telegram_id=telegram_id)
                session.add(user)
            return user

    async def get(self, telegram_id: int) -> User | None:
        async with self._sessions() as session:
            user: User | None = await session.get(User, telegram_id)
            return user

    async def get_token_blob(self, telegram_id: int) -> tuple[bytes | None, datetime | None]:
        async with self._sessions() as session:
            user: User | None = await session.get(User, telegram_id)
            if user is None:
                return None, None
            return user.silpo_tokens, user.silpo_token_expires_at

    async def set_token_blob(
        self, telegram_id: int, blob: bytes, expires_at: datetime | None
    ) -> None:
        async with self._sessions() as session, session.begin():
            user: User | None = await session.get(User, telegram_id)
            if user is None:
                user = User(telegram_id=telegram_id)
                session.add(user)
            user.silpo_tokens = blob
            user.silpo_token_expires_at = expires_at

    async def clear_tokens(self, telegram_id: int) -> None:
        """Forces the user through account linking again."""
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(silpo_tokens=None, silpo_token_expires_at=None)
            )

    async def set_budget(self, telegram_id: int, budget_weekly: int | None) -> None:
        """`None` clears the cap — the budget pass then leaves carts alone entirely."""
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(budget_weekly=budget_weekly)
            )

    async def linked(self) -> list[int]:
        """Everyone holding Silpo tokens — what a background import iterates."""
        async with self._sessions() as session:
            result = await session.execute(
                select(User.telegram_id).where(User.silpo_tokens.is_not(None))
            )
            return [int(row) for row in result.scalars()]

    async def delete(self, telegram_id: int) -> bool:
        """«/delete» — the user row and everything hanging off it, in one statement.

        Every other table cascades on `users.telegram_id`, which is what makes this a
        wipe rather than a list of tables to remember. SQLite only honours the cascade
        with foreign keys switched on, so the children are deleted explicitly first.
        """
        async with self._sessions() as session, session.begin():
            if await session.get(User, telegram_id) is None:
                return False
            for table in (
                Notification,
                HistoryImport,
                HabitMute,
                ProductHabit,
                Purchase,
                ConversationMessage,
            ):
                await session.execute(delete(table).where(table.user_id == telegram_id))
            baskets = (
                select(DraftBasketRow.id)
                .where(DraftBasketRow.user_id == telegram_id)
                .scalar_subquery()
            )
            await session.execute(delete(DraftItem).where(DraftItem.basket_id.in_(baskets)))
            await session.execute(
                delete(DraftBasketRow).where(DraftBasketRow.user_id == telegram_id)
            )
            await session.execute(delete(User).where(User.telegram_id == telegram_id))
            return True

    async def set_quiet_hours(self, telegram_id: int, start: int | None, end: int | None) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(quiet_from=start, quiet_to=end)
            )

    async def set_branch(self, telegram_id: int, branch_id: str) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(User).where(User.telegram_id == telegram_id).values(branch_id=branch_id)
            )


class OAuthClientRepo:
    """The app-wide DCR registration. Exactly one row, shared by every user."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = await session.get(OAuthClientRegistration, _REGISTRATION_ID)
            if row is None:
                return None
            loaded: dict[str, Any] = json.loads(row.payload)
            return loaded

    async def set(self, payload: dict[str, Any]) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.get(OAuthClientRegistration, _REGISTRATION_ID)
            if row is None:
                session.add(
                    OAuthClientRegistration(id=_REGISTRATION_ID, payload=json.dumps(payload))
                )
            else:
                row.payload = json.dumps(payload)

    async def clear(self) -> None:
        """Recovery path: an expired DCR secret is otherwise unrecoverable (mcp #3256)."""
        async with self._sessions() as session, session.begin():
            await session.execute(
                delete(OAuthClientRegistration).where(
                    OAuthClientRegistration.id == _REGISTRATION_ID
                )
            )


class ConversationRepo:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(self, telegram_id: int, role: str, content: str) -> None:
        async with self._sessions() as session, session.begin():
            session.add(ConversationMessage(user_id=telegram_id, role=role, content=content))

    async def last_n(self, telegram_id: int, n: int = 20) -> list[ConversationMessage]:
        """The most recent `n` messages, oldest first — the order an LLM expects."""
        async with self._sessions() as session:
            result = await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.user_id == telegram_id)
                .order_by(ConversationMessage.id.desc())
                .limit(n)
            )
            return list(reversed(result.scalars().all()))


def _line(item: DraftItem) -> ResolvedLine:
    """One stored row back into the domain object the passes work with."""
    return ResolvedLine(
        description=item.description,
        category=item.category,
        product_id=item.product_id,
        company_id=item.company_id,
        branch_id=item.branch_id,
        name=item.name,
        qty=item.qty,
        unit=item.unit,
        unit_price=Decimal(str(item.unit_price)),
        old_price=(Decimal(str(item.old_price)) if item.old_price is not None else None),
        reason_kind=item.reason_kind,
        reason_text=item.reason_text,
        substituted_from=item.substituted_from,
        optional=item.optional,
        unavailable=item.unavailable,
        weighted=item.weighted,
        step=item.step,
        stock=item.stock,
        synced=item.synced,
    )


class BasketRepo:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_from_cart(
        self, telegram_id: int, title: str, intent: str, cart: ResolvedCart
    ) -> int:
        """Persist a resolved cart as the user's active draft.

        Any previous draft is discarded: two live drafts would make "confirm" ambiguous.
        """
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(DraftBasketRow)
                .where(
                    DraftBasketRow.user_id == telegram_id,
                    DraftBasketRow.status == "draft",
                )
                .values(status="discarded")
            )

            basket = DraftBasketRow(
                user_id=telegram_id,
                title=title,
                intent=intent,
                status="draft",
                total=cart.total,
                estimated_savings=cart.estimated_savings,
                savings_notes=json.dumps(cart.savings_notes, ensure_ascii=False),
                coupon_notes=json.dumps(cart.coupon_notes, ensure_ascii=False),
                removals=json.dumps([r.model_dump() for r in cart.removals], ensure_ascii=False),
                warnings=json.dumps(cart.warnings, ensure_ascii=False),
            )
            session.add(basket)
            await session.flush()

            for position, line in enumerate(cart.lines):
                session.add(
                    DraftItem(
                        basket_id=basket.id,
                        position=position,
                        description=line.description,
                        category=line.category,
                        product_id=line.product_id,
                        company_id=line.company_id,
                        branch_id=line.branch_id,
                        name=line.name,
                        qty=line.qty,
                        unit=line.unit,
                        unit_price=line.unit_price,
                        old_price=line.old_price,
                        reason_kind=line.reason_kind,
                        reason_text=line.reason_text,
                        substituted_from=line.substituted_from,
                        optional=line.optional,
                        unavailable=line.unavailable,
                        weighted=line.weighted,
                        step=line.step,
                        stock=line.stock,
                    )
                )
            return basket.id

    async def get_active(self, telegram_id: int) -> DraftBasketRow | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(DraftBasketRow)
                .where(
                    DraftBasketRow.user_id == telegram_id,
                    DraftBasketRow.status == "draft",
                )
                .order_by(DraftBasketRow.id.desc())
                .limit(1)
            )
            basket: DraftBasketRow | None = result.scalar_one_or_none()
            return basket

    async def load_cart(self, basket_id: int) -> ResolvedCart | None:
        """Rebuild the domain object — the bot reloads a draft when the user confirms."""
        async with self._sessions() as session:
            basket = await session.get(DraftBasketRow, basket_id)
            if basket is None:
                return None
            result = await session.execute(
                select(DraftItem)
                .where(DraftItem.basket_id == basket_id, DraftItem.removed.is_(False))
                .order_by(DraftItem.position)
            )
            return ResolvedCart(
                lines=[_line(item) for item in result.scalars().all()],
                total=Decimal(str(basket.total)),
                estimated_savings=Decimal(str(basket.estimated_savings)),
                savings_notes=json.loads(basket.savings_notes),
                coupon_notes=json.loads(basket.coupon_notes),
                removals=[CartRemoval.model_validate(r) for r in json.loads(basket.removals)],
                warnings=json.loads(basket.warnings),
            )

    async def synced_lines(
        self, telegram_id: int, baskets: int = SYNCED_BASKETS
    ) -> list[ResolvedLine]:
        """Everything Komora has put in this user's Silpo cart recently, newest first.

        The candidate set for «прибери…» — and the only one. Komora never offers to
        remove a product it did not add, because it cannot tell one the user chose in
        the Silpo app from one of its own, and guessing wrong deletes real food.

        Selected by `DraftItem.synced`, which is set from what a push actually landed.
        The basket's own status used to stand in for that, and it is not the same
        claim: a push that lands partly leaves a `draft`, so the lines that really did
        reach the cart were invisible here and «прибери молоко» could not name a
        product Komora had put there minutes earlier. Unavailable lines never carry
        the flag, because they are never sent.

        **`removed` is deliberately not a filter here.** ✕ on a draft row hides it from
        the draft; it does nothing to the Silpo cart, and the row's `synced` flag is
        the record that the product is still there. Filtering on `removed` made a
        product Komora had put in the cart, and the user then struck off the draft,
        impossible to name in the chat — «прибери молоко» found no candidate, so the
        one surface that could take it back out said it had nothing to remove.
        """
        async with self._sessions() as session:
            recent = (
                select(DraftItem.basket_id)
                .join(DraftBasketRow, DraftBasketRow.id == DraftItem.basket_id)
                .where(
                    DraftBasketRow.user_id == telegram_id,
                    DraftItem.synced.is_(True),
                )
                .group_by(DraftItem.basket_id)
                .order_by(DraftItem.basket_id.desc())
                .limit(baskets)
                .scalar_subquery()
            )
            result = await session.execute(
                select(DraftItem)
                .where(
                    DraftItem.basket_id.in_(recent),
                    DraftItem.synced.is_(True),
                )
                .order_by(DraftItem.basket_id.desc(), DraftItem.position)
            )
            return [_line(item) for item in result.scalars().all()]

    async def replace_item(self, basket_id: int, position: int, line: ResolvedLine) -> bool:
        """Swap one line's product, keeping its place in the basket.

        Used by «інший варіант». The description is left untouched: it is the query
        the alternatives came from, and the next tap needs it again.

        `position` is an index into the lines `load_cart` returned, which is what the
        «⇄ N» button carries — not the `position` column. The two agree only while no
        row is `removed`, and matching on the column instead would edit the wrong
        product the moment one is: `load_cart` filters those out, so every line below a
        removed one sits at a lower index than its stored position. `drop_item` sets
        that flag now, so the two disagree in ordinary use — `_visible_item` is the one
        selection all three share, and `load_cart` filters identically.
        """
        async with self._sessions() as session, session.begin():
            item = await self._visible_item(session, basket_id, position)
            if item is None:
                return False
            item.product_id = line.product_id
            item.company_id = line.company_id
            item.branch_id = line.branch_id
            item.name = line.name
            item.qty = line.qty
            item.unit = line.unit
            item.unit_price = line.unit_price
            item.old_price = line.old_price
            item.unavailable = line.unavailable
            item.substituted_from = line.substituted_from
            item.weighted = line.weighted
            item.step = line.step
            item.stock = line.stock
            return True

    async def set_qty(self, basket_id: int, position: int, qty: float) -> bool:
        """Set one line's quantity.

        The same index space as `load_cart` — visible lines in stored order — so a
        surface that just rendered the basket can address it safely.
        """
        async with self._sessions() as session, session.begin():
            item = await self._visible_item(session, basket_id, position)
            if item is None:
                return False
            item.qty = qty
            return True

    async def drop_item(self, basket_id: int, position: int) -> bool:
        """Mark one line removed. The row stays (history keeps what was synced) but
        `load_cart` filters it out from here on.

        Only the draft changes. A row that already landed in the Silpo cart keeps its
        `synced` flag, and `synced_lines` keeps offering it — striking a product off
        the draft is not the same as taking it out of the cart, and the chat is the
        surface that can do the second.
        """
        async with self._sessions() as session, session.begin():
            item = await self._visible_item(session, basket_id, position)
            if item is None:
                return False
            item.removed = True
            return True

    @staticmethod
    async def _visible_item(
        session: AsyncSession, basket_id: int, position: int
    ) -> DraftItem | None:
        """The one selection `replace_item`, `set_qty` and `drop_item` share.

        A negative position is nobody's line. SQLite reads `OFFSET -1` as `OFFSET 0`
        and hands back the FIRST row — so `POST …/lines/-1/remove`, the one caller
        that had no bounds check of its own, deleted a line the request never named
        and answered 200. Postgres raises on the same query instead, which would have
        made it a 500 rather than a wrong answer. Refused here, where all three see
        it, rather than three times over at the call sites.
        """
        if position < 0:
            return None
        result = await session.execute(
            select(DraftItem)
            .where(DraftItem.basket_id == basket_id, DraftItem.removed.is_(False))
            .order_by(DraftItem.position)
            .offset(position)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def mark_synced(self, basket_id: int, product_ids: set[str]) -> None:
        """Record which of this basket's lines actually reached the Silpo cart.

        Keyed on `product_id` rather than position because that is what
        `execute_sync` can vouch for: it judges a write by reading the cart back, and
        what it reads back are product ids. Positions would have to survive an edit
        between the two taps; ids are what Silpo itself holds.
        """
        if not product_ids:
            return
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(DraftItem)
                .where(
                    DraftItem.basket_id == basket_id,
                    DraftItem.product_id.in_(product_ids),
                )
                .values(synced=True, synced_at=utcnow())
            )

    async def unmark_synced(self, telegram_id: int, product_ids: set[str]) -> None:
        """A product Komora took back out of the cart is no longer in it.

        Scoped to the user, not to a basket: a removal targets whatever earlier basket
        put the product there, and `_push` knows the sender rather than that basket.
        Nothing user-visible rests on this — `match_removals` gates every candidate
        against a live cart read — but a flag that says "this is in your Silpo cart"
        must not go on saying it after Komora itself removed it.
        """
        if not product_ids:
            return
        async with self._sessions() as session, session.begin():
            baskets = (
                select(DraftBasketRow.id)
                .where(DraftBasketRow.user_id == telegram_id)
                .scalar_subquery()
            )
            await session.execute(
                update(DraftItem)
                .where(
                    DraftItem.basket_id.in_(baskets),
                    DraftItem.product_id.in_(product_ids),
                )
                .values(synced=False, synced_at=None)
            )

    async def synced_at(self, telegram_id: int) -> dict[str, datetime]:
        """When Komora last put each product in this user's Silpo cart, for lines still
        recorded as there. What a nudge asks before calling something «закінчується»."""
        async with self._sessions() as session:
            result = await session.execute(
                select(DraftItem.product_id, func.max(DraftItem.synced_at))
                .join(DraftBasketRow, DraftBasketRow.id == DraftItem.basket_id)
                .where(
                    DraftBasketRow.user_id == telegram_id,
                    DraftItem.synced.is_(True),
                    DraftItem.synced_at.is_not(None),
                )
                .group_by(DraftItem.product_id)
            )
            return {str(product_id): at for product_id, at in result.all() if at is not None}

    async def update_totals(self, basket_id: int, cart: ResolvedCart) -> None:
        """Write back everything a swap recomputes.

        An earlier version persisted only the total and the savings figure, so the
        stored notes kept naming the product that had just been replaced — the draft
        disagreed with itself the moment it was reloaded.
        """
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(DraftBasketRow)
                .where(DraftBasketRow.id == basket_id)
                .values(
                    total=cart.total,
                    estimated_savings=cart.estimated_savings,
                    savings_notes=json.dumps(cart.savings_notes, ensure_ascii=False),
                    coupon_notes=json.dumps(cart.coupon_notes, ensure_ascii=False),
                )
            )

    async def set_status(self, basket_id: int, status: BasketStatus) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(DraftBasketRow).where(DraftBasketRow.id == basket_id).values(status=status)
            )

    async def get(self, basket_id: int) -> DraftBasketRow | None:
        """The row itself — callers need `user_id` to check a callback's owner.

        A Telegram callback carries a basket id chosen by the client, so acting on one
        without checking who owns it would let any user sync anyone's basket.
        """
        async with self._sessions() as session:
            row: DraftBasketRow | None = await session.get(DraftBasketRow, basket_id)
            return row

    async def get_status(self, basket_id: int) -> str | None:
        async with self._sessions() as session:
            basket = await session.get(DraftBasketRow, basket_id)
            return basket.status if basket else None


# --- Habits ----------------------------------------------------------------------


class PurchaseRepo:
    """The habits input. Idempotent by construction: re-importing changes nothing."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def upsert(self, user_id: int, events: Sequence[PurchaseEvent]) -> int:
        """Insert new events, refresh existing ones; returns how many rows were touched.

        The unique key is `(user, source, receipt, product)`. A receipt that lists a
        product twice was already netted into one event, so a collision is the same
        event seen again — replaced, never summed.
        """
        if not events:
            return 0
        async with self._sessions() as session, session.begin():
            keys = {(e.source, e.receipt_key, e.product_key) for e in events}
            result = await session.execute(
                select(Purchase).where(
                    Purchase.user_id == user_id,
                    Purchase.receipt_key.in_({k[1] for k in keys}),
                )
            )
            existing = {
                (row.source, row.receipt_key, row.product_key): row for row in result.scalars()
            }
            touched = 0
            for event in events:
                row = existing.get((event.source, event.receipt_key, event.product_key))
                if row is None:
                    row = Purchase(
                        user_id=user_id,
                        source=event.source,
                        receipt_key=event.receipt_key,
                        product_key=event.product_key,
                    )
                    session.add(row)
                    existing[(event.source, event.receipt_key, event.product_key)] = row
                row.name = event.name
                row.qty = event.qty
                row.unit = event.unit
                row.unit_price = event.unit_price
                row.weighted = event.weighted
                row.reorderable = event.reorderable
                row.bought_at = event.bought_at
                if event.external_product_id is not None:
                    row.external_product_id = event.external_product_id
                touched += 1
            return touched

    async def events(self, user_id: int) -> list[PurchaseEvent]:
        async with self._sessions() as session:
            result = await session.execute(
                select(Purchase).where(Purchase.user_id == user_id).order_by(Purchase.bought_at)
            )
            return [_event_of(row) for row in result.scalars()]

    async def learn_external_id(self, user_id: int, product_key: str, external_id: int) -> None:
        """A search that found the stored id teaches the article number back, so the
        next draft searches exactly rather than by name."""
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(Purchase)
                .where(
                    Purchase.user_id == user_id,
                    Purchase.product_key == product_key,
                    Purchase.external_product_id.is_(None),
                )
                .values(external_product_id=external_id)
            )
            await session.execute(
                update(ProductHabit)
                .where(
                    ProductHabit.user_id == user_id,
                    ProductHabit.product_key == product_key,
                    ProductHabit.external_product_id.is_(None),
                )
                .values(external_product_id=external_id)
            )


def _event_of(row: Purchase) -> PurchaseEvent:
    source: Source = "online" if row.source == "online" else "offline"
    return PurchaseEvent(
        source=source,
        receipt_key=row.receipt_key,
        product_key=row.product_key,
        name=row.name,
        qty=row.qty,
        unit=row.unit,
        unit_price=Decimal(str(row.unit_price)),
        weighted=row.weighted,
        reorderable=row.reorderable,
        bought_at=row.bought_at,
        external_product_id=row.external_product_id,
    )


class HabitRepo:
    """`product_habits` is replaced on every recompute; `habit_mutes` is user state and
    survives it — which is why the two are separate tables."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def replace(self, user_id: int, habits: Sequence[Habit]) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(delete(ProductHabit).where(ProductHabit.user_id == user_id))
            now = utcnow()
            for habit in habits:
                session.add(
                    ProductHabit(
                        user_id=user_id,
                        product_key=habit.product_key,
                        name=habit.name,
                        external_product_id=habit.external_product_id,
                        unit=habit.unit,
                        weighted=habit.weighted,
                        reorderable=habit.reorderable,
                        events=habit.events,
                        median_gap_days=habit.median_gap_days,
                        cv=habit.cv,
                        confidence=habit.confidence,
                        last_bought_on=habit.last_bought,
                        last_qty=habit.last_qty,
                        median_qty=habit.median_qty,
                        due_on=habit.due_on,
                        computed_at=now,
                    )
                )

    async def list(self, user_id: int) -> list[Habit]:
        """Every tracked habit with its mute flag joined in, most confident first."""
        async with self._sessions() as session:
            muted = {
                key
                for key in (
                    await session.execute(
                        select(HabitMute.product_key).where(HabitMute.user_id == user_id)
                    )
                ).scalars()
            }
            result = await session.execute(
                select(ProductHabit)
                .where(ProductHabit.user_id == user_id)
                .order_by(ProductHabit.confidence.desc(), ProductHabit.due_on)
            )
            return [
                Habit(
                    product_key=row.product_key,
                    name=row.name,
                    events=row.events,
                    median_gap_days=row.median_gap_days,
                    cv=row.cv,
                    confidence=row.confidence,
                    last_bought=row.last_bought_on,
                    last_qty=row.last_qty,
                    median_qty=row.median_qty,
                    due_on=row.due_on,
                    unit=row.unit,
                    weighted=row.weighted,
                    reorderable=row.reorderable,
                    external_product_id=row.external_product_id,
                    muted=row.product_key in muted,
                )
                for row in result.scalars()
            ]

    async def set_muted(self, user_id: int, product_key: str, muted: bool) -> bool:
        """Returns False when the user has no such habit — a key from the client is
        no more proof of anything than a basket id was."""
        async with self._sessions() as session, session.begin():
            known = await session.execute(
                select(ProductHabit.id).where(
                    ProductHabit.user_id == user_id, ProductHabit.product_key == product_key
                )
            )
            if known.first() is None:
                return False
            await session.execute(
                delete(HabitMute).where(
                    HabitMute.user_id == user_id, HabitMute.product_key == product_key
                )
            )
            if muted:
                session.add(HabitMute(user_id=user_id, product_key=product_key))
            return True


ImportOutcome = Literal["ok", "skipped", "failed"]


class HistoryImportRepo:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self,
        user_id: int,
        source: str,
        outcome: ImportOutcome,
        detail: str = "",
        at: datetime | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            session.add(
                HistoryImport(
                    user_id=user_id,
                    source=source,
                    outcome=outcome,
                    detail=detail,
                    at=at or utcnow(),
                )
            )

    async def last_ok(self, user_id: int, source: str) -> datetime | None:
        """Freshness: when this source was last read successfully."""
        async with self._sessions() as session:
            result = await session.execute(
                select(HistoryImport.at)
                .where(
                    HistoryImport.user_id == user_id,
                    HistoryImport.source == source,
                    HistoryImport.outcome == "ok",
                )
                .order_by(HistoryImport.at.desc())
                .limit(1)
            )
            at: datetime | None = result.scalar_one_or_none()
            return at


class NotificationRepo:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def last_sent(self, user_id: int, kind: str, subject_key: str) -> datetime | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(Notification.sent_at)
                .where(
                    Notification.user_id == user_id,
                    Notification.kind == kind,
                    Notification.subject_key == subject_key,
                )
                .order_by(Notification.sent_at.desc())
                .limit(1)
            )
            at: datetime | None = result.scalar_one_or_none()
            return at

    async def record(
        self, user_id: int, kind: str, subject_keys: Sequence[str], at: datetime | None = None
    ) -> None:
        async with self._sessions() as session, session.begin():
            now = at or utcnow()
            for key in subject_keys:
                session.add(Notification(user_id=user_id, kind=kind, subject_key=key, sent_at=now))
