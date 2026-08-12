"""removals on a draft basket

Revision ID: 885ff3e61e67
Revises: 7aa53814a7dd
Create Date: 2026-08-12 15:42:59.026273

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# Custom TypeDecorators (e.g. UtcDateTime) are rendered fully qualified by
# autogenerate, so the module must be importable in every migration.


# revision identifiers, used by Alembic.
revision: str = "885ff3e61e67"
down_revision: str | Sequence[str] | None = "7aa53814a7dd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    `server_default` is hand-added, for the **fourth** time — autogenerate emitted
    `nullable=False` with no default again. A model-side default produces no DDL, so
    the generated version applies cleanly to an empty database and fails on every one
    that holds a row. `tests/test_migrations.py` now fails on this pattern instead of
    a live database doing it.
    """
    with op.batch_alter_table("draft_baskets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("removals", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("draft_baskets", schema=None) as batch_op:
        batch_op.drop_column("removals")
