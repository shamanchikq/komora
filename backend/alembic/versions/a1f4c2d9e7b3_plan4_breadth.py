"""plan 4: pack size and special prices on draft items, a menu on baskets, the digest
flag, price snapshots and receipt totals

Revision ID: a1f4c2d9e7b3
Revises: 4c7e1f9a2b3d
Create Date: 2026-09-14 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom TypeDecorators (e.g. UtcDateTime) are rendered fully qualified by
# autogenerate, so the module must be importable in every migration.
import komora.db.base

# revision identifiers, used by Alembic.
revision: str = "a1f4c2d9e7b3"
down_revision: str | Sequence[str] | None = "4c7e1f9a2b3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Every NOT NULL column added to an existing table carries a `server_default` — the
    trap `tests/test_migrations.py` exists for: autogenerate omits it whenever the
    model has a Python-side default, and the migration then fails on the first
    database with a row in it.
    """
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("digest_weekly", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    with op.batch_alter_table("draft_baskets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("menu", sa.Text(), nullable=False, server_default="[]"))

    with op.batch_alter_table("draft_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("display_ratio", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("display_price", sa.Numeric(10, 2), nullable=True))
        batch_op.add_column(
            sa.Column("special_prices", sa.Text(), nullable=False, server_default="[]")
        )

    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("product_key", sa.String(length=64), nullable=False),
        sa.Column("external_product_id", sa.BigInteger(), nullable=True),
        sa.Column("branch_id", sa.String(length=64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("old_price", sa.Numeric(10, 2), nullable=True),
        sa.Column("display_price", sa.Numeric(10, 2), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("captured_at", komora.db.base.UtcDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "product_key", "branch_id", "day", name="uq_price_snapshot"),
    )
    with op.batch_alter_table("price_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            "ix_price_snapshots_user_product", ["user_id", "product_key"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_price_snapshots_user_id"), ["user_id"], unique=False)

    op.create_table(
        "receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("receipt_key", sa.String(length=80), nullable=False),
        sa.Column("bought_at", komora.db.base.UtcDateTime(timezone=True), nullable=False),
        sa.Column("total", sa.Numeric(10, 2), nullable=False),
        sa.Column("discount", sa.Numeric(10, 2), nullable=False),
        sa.Column("bonuses_accrued", sa.Numeric(10, 2), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "receipt_key", name="uq_receipt"),
    )
    with op.batch_alter_table("receipts", schema=None) as batch_op:
        batch_op.create_index("ix_receipts_user_bought_at", ["user_id", "bought_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_receipts_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("receipts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_receipts_user_id"))
        batch_op.drop_index("ix_receipts_user_bought_at")
    op.drop_table("receipts")

    with op.batch_alter_table("price_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_price_snapshots_user_id"))
        batch_op.drop_index("ix_price_snapshots_user_product")
    op.drop_table("price_snapshots")

    with op.batch_alter_table("draft_items", schema=None) as batch_op:
        batch_op.drop_column("special_prices")
        batch_op.drop_column("display_price")
        batch_op.drop_column("display_ratio")

    with op.batch_alter_table("draft_baskets", schema=None) as batch_op:
        batch_op.drop_column("menu")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("digest_weekly")
