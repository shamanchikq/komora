"""Database tables.

Only Postgres-compatible types are used: SQLite is the v1 store, but nothing here
should have to change to move.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Index, LargeBinary, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from komora.db.base import Base, utcnow


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    silpo_tokens: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    """Encrypted OAuth token payload. Never stored in the clear — see core.crypto."""
    silpo_token_expires_at: Mapped[datetime | None] = mapped_column(default=None)
    """Absolute expiry. The mcp SDK's OAuthToken carries only a relative `expires_in`,
    which cannot be reconstructed after a restart; this is what the #3250 workaround
    reads back so refresh works instead of prompting the user to log in again."""

    branch_id: Mapped[str | None] = mapped_column(String(64), default=None)
    budget_weekly: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class OAuthClientRegistration(Base):
    """The Dynamic Client Registration issued by Silpo — ONE row for the whole app.

    Deliberately not keyed by user. Per-user rows would register a fresh OAuth client
    with Silpo for every Telegram user, which invites rate-limiting or a ban.
    """

    __tablename__ = "oauth_client_registration"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False, default=1)
    payload: Mapped[str] = mapped_column(Text)
    """OAuthClientInformationFull, JSON-encoded."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConversationMessage(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_id_id", "user_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DraftBasketRow(Base):
    __tablename__ = "draft_baskets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    intent: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    """draft | confirmed | synced | discarded"""

    total: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    estimated_savings: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    savings_notes: Mapped[str] = mapped_column(Text, default="[]")
    coupon_notes: Mapped[str] = mapped_column(Text, default="[]")
    """Kept apart from savings_notes: a swap regenerates one and must not touch
    the other."""
    removals: Mapped[str] = mapped_column(Text, default="[]")
    """`CartRemoval` list, JSON-encoded — what confirming this basket takes OUT of the
    Silpo cart. Persisted rather than recomputed because the two taps that authorise it
    happen in different turns, and the second one must send exactly what the first one
    showed."""
    warnings: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DraftItem(Base):
    __tablename__ = "draft_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    basket_id: Mapped[int] = mapped_column(
        ForeignKey("draft_baskets.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(default=0)
    """Preserves the order the passes produced; the user reads it top to bottom."""

    description: Mapped[str] = mapped_column(Text, default="")
    """What the model asked for. Kept so a line can be re-resolved after the fact —
    «інший варіант» re-runs this query rather than storing every candidate."""
    category: Mapped[str | None] = mapped_column(Text, default=None)
    """The Silpo category the line resolved from, so a swap stays on the same shelf."""

    product_id: Mapped[str] = mapped_column(String(64))
    company_id: Mapped[str] = mapped_column(String(64))
    branch_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(Text)
    qty: Mapped[float]
    unit: Mapped[str] = mapped_column(String(64))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    """Silpo's pre-discount price, so a reloaded draft still shows what was saved."""

    reason_kind: Mapped[str] = mapped_column(String(16))
    reason_text: Mapped[str] = mapped_column(Text)
    substituted_from: Mapped[str | None] = mapped_column(Text, default=None)
    optional: Mapped[bool] = mapped_column(default=False)
    unavailable: Mapped[bool] = mapped_column(default=False)
    removed: Mapped[bool] = mapped_column(default=False)
    synced: Mapped[bool] = mapped_column(default=False, index=True)
    """This line is in the user's real Silpo cart, because a push put it there.

    Per line rather than per basket because a push can land partly: `execute_sync`
    reports what actually arrived, and a basket holding one landed line and one
    rejected one stays a `draft` so it can be retried. Recording it only as
    `status == "synced"` made that basket indistinguishable from one that had never
    been sent — it reopened saying «у кошику Сільпо нічого не зміниться» with a
    product of its own already sitting there, and `synced_lines` could not offer that
    product for removal either.
    """
    weighted: Mapped[bool] = mapped_column(default=False)
    """Priced per kilogram; a Mini App needs this to show «0,15 кг × 999,00 ₴/кг»."""
    step: Mapped[float | None] = mapped_column(default=None)
    stock: Mapped[float | None] = mapped_column(default=None)
