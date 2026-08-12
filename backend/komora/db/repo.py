"""Repositories — the only place SQL is written.

Each method opens its own session, so callers never manage transactions.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from komora.core.models import BasketStatus, ResolvedCart, ResolvedLine
from komora.db.tables import (
    ConversationMessage,
    DraftBasketRow,
    DraftItem,
    OAuthClientRegistration,
    User,
)

_REGISTRATION_ID = 1


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
            lines = [
                ResolvedLine(
                    description=item.description,
                    category=item.category,
                    product_id=item.product_id,
                    company_id=item.company_id,
                    branch_id=item.branch_id,
                    name=item.name,
                    qty=item.qty,
                    unit=item.unit,
                    unit_price=Decimal(str(item.unit_price)),
                    old_price=(
                        Decimal(str(item.old_price)) if item.old_price is not None else None
                    ),
                    reason_kind=item.reason_kind,
                    reason_text=item.reason_text,
                    substituted_from=item.substituted_from,
                    optional=item.optional,
                    unavailable=item.unavailable,
                )
                for item in result.scalars().all()
            ]
            return ResolvedCart(
                lines=lines,
                total=Decimal(str(basket.total)),
                estimated_savings=Decimal(str(basket.estimated_savings)),
                savings_notes=json.loads(basket.savings_notes),
                coupon_notes=json.loads(basket.coupon_notes),
                warnings=json.loads(basket.warnings),
            )

    async def replace_item(self, basket_id: int, position: int, line: ResolvedLine) -> bool:
        """Swap one line's product, keeping its place in the basket.

        Used by «інший варіант». The description is left untouched: it is the query
        the alternatives came from, and the next tap needs it again.
        """
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                select(DraftItem).where(
                    DraftItem.basket_id == basket_id, DraftItem.position == position
                )
            )
            item = result.scalar_one_or_none()
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
            return True

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
