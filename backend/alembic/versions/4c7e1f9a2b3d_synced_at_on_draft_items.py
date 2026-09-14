"""synced_at on draft items

Revision ID: 4c7e1f9a2b3d
Revises: e3460e4eec92
Create Date: 2026-09-14 16:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom TypeDecorators (e.g. UtcDateTime) are rendered fully qualified by
# autogenerate, so the module must be importable in every migration.
import komora.db.base

# revision identifiers, used by Alembic.
revision: str = "4c7e1f9a2b3d"
down_revision: str | Sequence[str] | None = "e3460e4eec92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Nullable, so no `server_default`. The backfill gives a line already flagged as in
    the cart its basket's `created_at` — the closest record there is. A basket is
    pushed minutes after it is built, and a nudge only asks whether the push came after
    the last purchase, so a few minutes early cannot change an answer.
    """
    with op.batch_alter_table("draft_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("synced_at", komora.db.base.UtcDateTime(timezone=True), nullable=True)
        )

    op.execute(
        sa.text(
            "UPDATE draft_items SET synced_at = "
            "(SELECT created_at FROM draft_baskets WHERE draft_baskets.id = draft_items.basket_id) "
            "WHERE synced = true"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("draft_items", schema=None) as batch_op:
        batch_op.drop_column("synced_at")
