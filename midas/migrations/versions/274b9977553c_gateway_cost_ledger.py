"""gateway cost ledger

Revision ID: 274b9977553c
Revises: 49050df2d6a8
Create Date: 2026-11-08 17:58:53.903578
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "274b9977553c"
down_revision: str | None = "49050df2d6a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cost_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("gateway_cost_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cost_events", schema=None) as batch_op:
        batch_op.drop_column("gateway_cost_usd")
