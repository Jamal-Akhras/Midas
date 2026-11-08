"""baseline

Revision ID: 68d81a876674
Revises:
Create Date: 2026-11-08 17:23:16.968809
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "68d81a876674"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "equity_points",
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "pipeline_runs",
        sa.Column(
            "kind",
            sa.Enum(
                "screen",
                "committee",
                "reevaluation",
                "portfolio_review",
                "post_mortem",
                name="runkind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "awaiting_approval",
                "done",
                "failed",
                "halted",
                "cancelled",
                name="runstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["positions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("pipeline_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_pipeline_runs_ticker"), ["ticker"], unique=False)

    op.create_table(
        "positions",
        sa.Column("thesis_id", sa.Uuid(), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column(
            "instrument",
            sa.Enum("shares", "option", name="instrument", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("sector", sa.String(length=50), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column("avg_price", sa.Float(), nullable=False),
        sa.Column("stop_price", sa.Float(), nullable=False),
        sa.Column("option_detail", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "closed", name="positionstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False),
        sa.Column("last_mark_price", sa.Float(), nullable=True),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["thesis_id"],
            ["trade_theses.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_positions_next_review_at"), ["next_review_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_positions_ticker"), ["ticker"], unique=False)

    op.create_table(
        "research_artifacts",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("agent_slug", sa.String(length=50), nullable=False),
        sa.Column(
            "artifact_type",
            sa.Enum(
                "screen_results",
                "fundamentals",
                "dossier",
                "catalysts",
                "triage",
                "bull_case",
                "bear_case",
                "structure",
                "risk_check",
                "verdict",
                "reevaluation",
                "portfolio_review",
                "post_mortem",
                name="artifacttype",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "review_status",
            sa.Enum(
                "unreviewed",
                "pending_approval",
                "approved",
                "rejected",
                name="reviewstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("model_used", sa.String(length=100), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("research_artifacts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_research_artifacts_artifact_type"), ["artifact_type"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_research_artifacts_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_research_artifacts_ticker"), ["ticker"], unique=False)

    op.create_table(
        "system_state",
        sa.Column("key", sa.String(length=60), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "trade_theses",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("verdict_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("bullish", "bearish", name="direction", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "instrument",
            sa.Enum("shares", "option", name="instrument", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("entry_zone_low", sa.Float(), nullable=False),
        sa.Column("entry_zone_high", sa.Float(), nullable=False),
        sa.Column("stop_price", sa.Float(), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("thesis_summary", sa.Text(), nullable=False),
        sa.Column("invalidators", sa.JSON(), nullable=False),
        sa.Column("review_schedule", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "proposed",
                "approved",
                "rejected",
                "executed",
                "closed",
                "cancelled",
                name="thesisstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["verdict_artifact_id"],
            ["research_artifacts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("trade_theses", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_trade_theses_ticker"), ["ticker"], unique=False)

    op.create_table(
        "approval_requests",
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("subject_type", sa.String(length=40), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "approved", "denied", name="approvalstatus", native_enum=False, length=20
            ),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("approval_requests", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_approval_requests_run_id"), ["run_id"], unique=False)

    op.create_table(
        "audit_events",
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "actor_type",
            sa.Enum("operator", "agent", "system", name="actortype", native_enum=False, length=20),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("subject_type", sa.String(length=40), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_audit_events_event_type"), ["event_type"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_audit_events_run_id"), ["run_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_audit_events_subject_id"), ["subject_id"], unique=False
        )

    op.create_table(
        "cost_events",
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("agent_slug", sa.String(length=50), nullable=False),
        sa.Column("task_type", sa.String(length=50), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("cost_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_cost_events_agent_slug"), ["agent_slug"], unique=False)
        batch_op.create_index(batch_op.f("ix_cost_events_run_id"), ["run_id"], unique=False)

    op.create_table(
        "data_snapshots",
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("tool_name", sa.String(length=60), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
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
    with op.batch_alter_table("data_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_data_snapshots_artifact_id"), ["artifact_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_data_snapshots_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_data_snapshots_ticker"), ["ticker"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_data_snapshots_tool_name"), ["tool_name"], unique=False
        )

    op.create_table(
        "option_structures",
        sa.Column("thesis_id", sa.Uuid(), nullable=False),
        sa.Column("underlying", sa.String(length=12), nullable=False),
        sa.Column(
            "strategy",
            sa.Enum(
                "long_call",
                "long_put",
                "call_debit_spread",
                "put_debit_spread",
                name="optionstrategy",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "option_type",
            sa.Enum("call", "put", name="optiontype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("dte_at_entry", sa.Integer(), nullable=False),
        sa.Column("long_strike", sa.Float(), nullable=False),
        sa.Column("short_strike", sa.Float(), nullable=True),
        sa.Column("est_premium_per_contract", sa.Float(), nullable=False),
        sa.Column("contracts", sa.Integer(), nullable=False),
        sa.Column("liquidity", sa.JSON(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["thesis_id"],
            ["trade_theses.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "review_checkpoints",
        sa.Column("position_id", sa.Uuid(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "trigger",
            sa.Enum(
                "scheduled",
                "earnings",
                "invalidation",
                "dte",
                "manual",
                "time_stop",
                name="checkpointtrigger",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "done", "skipped", name="checkpointstatus", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["research_artifacts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["positions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("review_checkpoints", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_review_checkpoints_position_id"), ["position_id"], unique=False
        )

    op.create_table(
        "screen_candidates",
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("price_at_screen", sa.Float(), nullable=False),
        sa.Column("fwd_10d_pct", sa.Float(), nullable=True),
        sa.Column("fwd_20d_pct", sa.Float(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("screen_candidates", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_screen_candidates_ticker"), ["ticker"], unique=False)

    op.create_table(
        "standing_stops",
        sa.Column("position_id", sa.Uuid(), nullable=False),
        sa.Column("alpaca_order_id", sa.String(length=64), nullable=False),
        sa.Column("stop_price", sa.Float(), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["positions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("standing_stops", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_standing_stops_position_id"), ["position_id"], unique=False
        )

    op.create_table(
        "trade_outcomes",
        sa.Column("position_id", sa.Uuid(), nullable=False),
        sa.Column("thesis_id", sa.Uuid(), nullable=False),
        sa.Column("pnl_usd", sa.Float(), nullable=False),
        sa.Column("pnl_pct", sa.Float(), nullable=False),
        sa.Column("holding_days", sa.Integer(), nullable=False),
        sa.Column("exit_kind", sa.String(length=20), nullable=False),
        sa.Column("bull_case_right", sa.Boolean(), nullable=True),
        sa.Column("bear_case_right", sa.Boolean(), nullable=True),
        sa.Column("invalidation_worked", sa.Boolean(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["positions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["thesis_id"],
            ["trade_theses.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("position_id"),
    )
    op.create_table(
        "model_calls",
        sa.Column("cost_event_id", sa.Uuid(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("user_prompt", sa.Text(), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("output_schema_name", sa.String(length=100), nullable=True),
        sa.Column("parse_ok", sa.Boolean(), nullable=False),
        sa.Column("router_decision", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["cost_event_id"],
            ["cost_events.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "orders",
        sa.Column("thesis_id", sa.Uuid(), nullable=False),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column("structure_id", sa.Uuid(), nullable=True),
        sa.Column(
            "broker",
            sa.Enum("paper_sim", "alpaca_paper", name="broker", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "side",
            sa.Enum("buy", "sell", name="orderside", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "instrument",
            sa.Enum("shares", "option", name="instrument", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column("limit_price", sa.Float(), nullable=True),
        sa.Column(
            "reason",
            sa.Enum(
                "entry", "exit", "trim", "roll", name="orderreason", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "proposed",
                "approved",
                "submitted",
                "filled",
                "rejected",
                "cancelled",
                name="orderstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("approved_by", sa.String(length=30), nullable=False),
        sa.Column("filled_price", sa.Float(), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("risk_check", sa.JSON(), nullable=False),
        sa.Column("alpaca_order_id", sa.String(length=64), nullable=False),
        sa.Column("fees_usd", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["positions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["structure_id"],
            ["option_structures.id"],
        ),
        sa.ForeignKeyConstraint(
            ["thesis_id"],
            ["trade_theses.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("orders")
    op.drop_table("model_calls")
    op.drop_table("trade_outcomes")
    with op.batch_alter_table("standing_stops", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_standing_stops_position_id"))

    op.drop_table("standing_stops")
    with op.batch_alter_table("screen_candidates", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_screen_candidates_ticker"))

    op.drop_table("screen_candidates")
    with op.batch_alter_table("review_checkpoints", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_review_checkpoints_position_id"))

    op.drop_table("review_checkpoints")
    op.drop_table("option_structures")
    with op.batch_alter_table("data_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_data_snapshots_tool_name"))
        batch_op.drop_index(batch_op.f("ix_data_snapshots_ticker"))
        batch_op.drop_index(batch_op.f("ix_data_snapshots_run_id"))
        batch_op.drop_index(batch_op.f("ix_data_snapshots_artifact_id"))

    op.drop_table("data_snapshots")
    with op.batch_alter_table("cost_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cost_events_run_id"))
        batch_op.drop_index(batch_op.f("ix_cost_events_agent_slug"))

    op.drop_table("cost_events")
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_events_subject_id"))
        batch_op.drop_index(batch_op.f("ix_audit_events_run_id"))
        batch_op.drop_index(batch_op.f("ix_audit_events_event_type"))

    op.drop_table("audit_events")
    with op.batch_alter_table("approval_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_approval_requests_run_id"))

    op.drop_table("approval_requests")
    with op.batch_alter_table("trade_theses", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_trade_theses_ticker"))

    op.drop_table("trade_theses")
    op.drop_table("system_state")
    with op.batch_alter_table("research_artifacts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_research_artifacts_ticker"))
        batch_op.drop_index(batch_op.f("ix_research_artifacts_run_id"))
        batch_op.drop_index(batch_op.f("ix_research_artifacts_artifact_type"))

    op.drop_table("research_artifacts")
    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_positions_ticker"))
        batch_op.drop_index(batch_op.f("ix_positions_next_review_at"))

    op.drop_table("positions")
    with op.batch_alter_table("pipeline_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pipeline_runs_ticker"))

    op.drop_table("pipeline_runs")
    op.drop_table("equity_points")
