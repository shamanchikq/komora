"""Repositories — the only place SQL is written.

Each method opens its own session, so callers never manage transactions.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from komora.core.models import BasketStatus, CartRemoval, ResolvedCart, ResolvedLine
from komora.db.tables import (
    ConversationMessage,
    DraftBasketRow,
    DraftItem,
    OAuthClientRegistration,
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
                .values(synced=True)
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
                .values(synced=False)
            )

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
