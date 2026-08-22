"""weighted step stock on draft items

Revision ID: 7d8c8868d8cc
Revises: 885ff3e61e67
Create Date: 2026-08-22 04:44:50.177942

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom TypeDecorators (e.g. UtcDateTime) are rendered fully qualified by
# autogenerate, so the module must be importable in every migration.
import komora.db.base  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "7d8c8868d8cc"
down_revision: str | Sequence[str] | None = "885ff3e61e67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    `server_default` by hand again — see 885ff3e61e67. A Mini App row needs to know
    weighted/step/stock to draw «0,15 кг × 999,00 ₴/кг» and to ceiling its stepper;
    the values ride on every search hit and were simply dropped before.
    """
    with op.batch_alter_table("draft_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("weighted", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("step", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("stock", sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("draft_items", schema=None) as batch_op:
        batch_op.drop_column("stock")
        batch_op.drop_column("step")
        batch_op.drop_column("weighted")
