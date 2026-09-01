"""synced flag on draft items

Revision ID: 9b41c07ae512
Revises: 7d8c8868d8cc
Create Date: 2026-09-01 15:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom TypeDecorators (e.g. UtcDateTime) are rendered fully qualified by
# autogenerate, so the module must be importable in every migration.
import komora.db.base  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "9b41c07ae512"
down_revision: str | Sequence[str] | None = "7d8c8868d8cc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    `server_default` by hand again — see 885ff3e61e67.

    The backfill is what makes the flag usable on an existing database: until now
    "Komora put this in the cart" was recorded only as `draft_baskets.status`, and
    `synced_lines` read it that way. A basket reaches that status only when
    `SyncReport.ok`, which means every sendable line landed — so its items are exactly
    the ones to mark, minus the unavailable ones, which are never sent at all.

    Partial syncs before this migration are not recoverable: they left a `draft` row
    that looks like any other, which is the bug this column exists to end.
    """
    with op.batch_alter_table("draft_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("synced", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.create_index(batch_op.f("ix_draft_items_synced"), ["synced"], unique=False)

    op.execute(
        sa.text(
            "UPDATE draft_items SET synced = true "
            "WHERE unavailable = false AND basket_id IN "
            "(SELECT id FROM draft_baskets WHERE status = 'synced')"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("draft_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_draft_items_synced"))
        batch_op.drop_column("synced")
