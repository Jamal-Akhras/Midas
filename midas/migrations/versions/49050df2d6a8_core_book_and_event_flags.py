"""core book and event flags

Revision ID: 49050df2d6a8
Revises: 68d81a876674
Create Date: 2026-11-08 17:47:12.272735
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "49050df2d6a8"
down_revision: str | None = "68d81a876674"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_flags",
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("event_date", sa.String(length=10), nullable=False),
        sa.Column("price_at_flag", sa.Float(), nullable=True),
        sa.Column("fwd_10d_pct", sa.Float(), nullable=True),
        sa.Column("fwd_20d_pct", sa.Float(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["research_artifacts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("event_flags", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_event_flags_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_event_flags_ticker"), ["ticker"], unique=False)

    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("book", sa.String(length=10), server_default="swing", nullable=False)
        )
        batch_op.create_index(batch_op.f("ix_positions_book"), ["book"], unique=False)

    with op.batch_alter_table("trade_theses", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("book", sa.String(length=10), server_default="swing", nullable=False)
        )
        batch_op.create_index(batch_op.f("ix_trade_theses_book"), ["book"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("trade_theses", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_trade_theses_book"))
        batch_op.drop_column("book")

    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_positions_book"))
        batch_op.drop_column("book")

    with op.batch_alter_table("event_flags", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_event_flags_ticker"))
        batch_op.drop_index(batch_op.f("ix_event_flags_run_id"))

    op.drop_table("event_flags")
