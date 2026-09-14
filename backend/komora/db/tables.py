"""Database tables.

Only Postgres-compatible types are used: SQLite is the v1 store, but nothing here
should have to change to move.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
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
    quiet_from: Mapped[int | None] = mapped_column(default=None)
    quiet_to: Mapped[int | None] = mapped_column(default=None)
    """Hours (Kyiv) between which a nudge is held back; `None` means the default
    window in `bot/habits_job.py`. A user's evening is not something to guess at."""
    digest_weekly: Mapped[bool] = mapped_column(default=False)
    """«/digest on» — the Sunday message (Plan 4 Task 4). Off by default, as spec
    §12.3 said: a message nobody asked for is the one kind Komora sends least."""
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
    menu: Mapped[str] = mapped_column(Text, default="[]")
    """`MenuItem` list, JSON-encoded — a meal plan's dishes, redrawn above the draft
    when it is reopened. Empty for every other intent."""
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
    synced_at: Mapped[datetime | None] = mapped_column(default=None)
    """When a push last landed this line. `synced` says the product is in the cart;
    this says since when — which is what lets a nudge tell «already in your cart» from
    «bought weeks ago». Null for lines landed before 2026-09-14."""
    weighted: Mapped[bool] = mapped_column(default=False)
    """Priced per kilogram; a Mini App needs this to show «0,15 кг × 999,00 ₴/кг»."""
    step: Mapped[float | None] = mapped_column(default=None)
    stock: Mapped[float | None] = mapped_column(default=None)
    display_ratio: Mapped[str | None] = mapped_column(String(32), default=None)
    """Silpo's pack size («900г»), since 2026-09-14. Null for older rows and for a
    product Silpo sent none for."""
    display_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    special_prices: Mapped[str] = mapped_column(Text, default="[]")
    """`SpecialPrice` list, JSON-encoded. A note, never a total."""


class Purchase(Base):
    """One product on one receipt or online order — the habits input.

    Kept as the normaliser produced it (`core/habits/purchases.py`): netted per
    receipt, bags and removed lines gone, times aware. `product_habits` is recomputed
    from here, never edited in place, so a rule change is a recompute and not a data
    migration. Nothing personal: no address, shop, city or receipt URL — the receipt is
    a hash of its token.
    """

    __tablename__ = "purchases"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "receipt_key", "product_key", name="uq_purchase"),
        Index("ix_purchases_user_id_bought_at", "user_id", "bought_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(8))
    """online | offline"""
    receipt_key: Mapped[str] = mapped_column(String(80))
    product_key: Mapped[str] = mapped_column(String(64))
    """Catalog product id, or `lager:<id>` for a receipt line with no catalog product."""
    external_product_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    """Silpo's numeric article — receipt `lagerId`, search hit `externalProductId`.
    Stored whenever it is learned: it is the one exact search key, and an online
    order never carries it."""
    name: Mapped[str] = mapped_column(Text)
    qty: Mapped[float]
    unit: Mapped[str] = mapped_column(String(64), default="")
    """«кг», or the pack size («400г») — the only thing that tells two sizes apart."""
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    weighted: Mapped[bool] = mapped_column(default=False)
    reorderable: Mapped[bool] = mapped_column(default=True)
    bought_at: Mapped[datetime]


class ProductHabit(Base):
    """The engine's output for one user, replaced wholesale on every recompute."""

    __tablename__ = "product_habits"
    __table_args__ = (UniqueConstraint("user_id", "product_key", name="uq_product_habit"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    product_key: Mapped[str] = mapped_column(String(64))
    group_key: Mapped[str | None] = mapped_column(String(64), default=None)
    """Reserved for grouping variants of one habit (Plan 3, option C). Unused: the
    only grouping measured did not group."""
    name: Mapped[str] = mapped_column(Text)
    external_product_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    unit: Mapped[str] = mapped_column(String(64), default="")
    weighted: Mapped[bool] = mapped_column(default=False)
    reorderable: Mapped[bool] = mapped_column(default=True)
    events: Mapped[int]
    median_gap_days: Mapped[float]
    cv: Mapped[float]
    confidence: Mapped[float]
    last_bought_on: Mapped[date]
    last_qty: Mapped[float]
    median_qty: Mapped[float]
    due_on: Mapped[date]
    computed_at: Mapped[datetime] = mapped_column(default=utcnow)


class HabitMute(Base):
    """«Не стежити» — user state, so it survives every recompute of `product_habits`."""

    __tablename__ = "habit_mutes"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), primary_key=True
    )
    product_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    muted_at: Mapped[datetime] = mapped_column(default=utcnow)


class HistoryImport(Base):
    """One row per import attempt per source: freshness is the last `ok`, and a skip is
    a row rather than a log line nobody reads."""

    __tablename__ = "history_imports"
    __table_args__ = (Index("ix_history_imports_user_source_at", "user_id", "source", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(8))
    at: Mapped[datetime] = mapped_column(default=utcnow)
    outcome: Mapped[str] = mapped_column(String(8))
    """ok | skipped | failed"""
    detail: Mapped[str] = mapped_column(Text, default="")


class PriceSnapshot(Base):
    """The shelf price of a tracked product on one day at one branch (Plan 4 Task 2).

    Per user, because the scan runs through the user's own session over the user's own
    tracked habits — a shared table keyed on branch would be a catalogue crawl by
    another name. Per branch, because prices differ by shop and a household that
    switches must not read the difference as a drop. `price` is the shelf price
    (after Silpo's own promotion), `old_price` the pre-promotion one when there is a
    promotion; `purchases.unit_price` is the *paid* price and is never mixed in.
    """

    __tablename__ = "price_snapshots"
    __table_args__ = (
        UniqueConstraint("user_id", "product_key", "branch_id", "day", name="uq_price_snapshot"),
        Index("ix_price_snapshots_user_product", "user_id", "product_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    product_key: Mapped[str] = mapped_column(String(64))
    external_product_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    branch_id: Mapped[str] = mapped_column(String(64))
    day: Mapped[date]
    """Kyiv day. One row per (product, branch, day), replaced on the same day."""
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    display_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    available: Mapped[bool] = mapped_column(default=True)
    captured_at: Mapped[datetime] = mapped_column(default=utcnow)


class Receipt(Base):
    """One loyalty-card receipt's totals (Plan 4 Task 4).

    `purchases` keeps lines; `sumReg`, `sumDiscount` and the bonuses accrued live on
    the receipt itself, and a digest's «витрачено» and «заощаджено» come from here —
    what Silpo actually charged, never a coupon inferred. Nothing personal: the key is
    the same hash `purchases.receipt_key` uses, and no shop, city or URL is kept.
    """

    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("user_id", "receipt_key", name="uq_receipt"),
        Index("ix_receipts_user_bought_at", "user_id", "bought_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    receipt_key: Mapped[str] = mapped_column(String(80))
    bought_at: Mapped[datetime]
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    """`sumReg` — undocumented; read as the receipt total, checked against the line
    sums by `core/habits/purchases.receipt_totals` and stored only when they agree
    within a hryvnia."""
    discount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    """`sumDiscount`."""
    bonuses_accrued: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    """`accruedBalaBonusesSum` — points, not hryvnias; never added to money."""


class Notification(Base):
    """What Komora said unasked, so it does not say it again inside the cooldown."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_kind_subject", "user_id", "kind", "subject_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    subject_key: Mapped[str] = mapped_column(String(64))
    sent_at: Mapped[datetime] = mapped_column(default=utcnow)
