"""Operational models: cost ledger, model-call archive, audit trail,
approvals. All append-only by convention (no update/delete paths exist)."""

import enum
import uuid
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from midas.models.base import Base, PKMixin, TimestampMixin


class CostEvent(PKMixin, TimestampMixin, Base):
    """Write-ahead cost log: recorded BEFORE parsed results reach callers.
    Failed parses and fallbacks are logged too - no untracked spend."""

    __tablename__ = "cost_events"

    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_runs.id"), index=True)
    agent_slug: Mapped[str] = mapped_column(String(50), default="", index=True)
    task_type: Mapped[str] = mapped_column(String(50), default="")
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(100))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)


class ModelCall(PKMixin, TimestampMixin, Base):
    """Every prompt and response, verbatim, plus the router's reasoning."""

    __tablename__ = "model_calls"

    cost_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cost_events.id"), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    user_prompt: Mapped[str] = mapped_column(Text, default="")
    messages: Mapped[list[Any]] = mapped_column(JSON, default=list)  # tool-loop transcript
    response_text: Mapped[str] = mapped_column(Text, default="")
    output_schema_name: Mapped[str | None] = mapped_column(String(100))
    parse_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    router_decision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ActorType(str, enum.Enum):
    operator = "operator"
    agent = "agent"
    system = "system"


class AuditEvent(PKMixin, TimestampMixin, Base):
    __tablename__ = "audit_events"

    event_type: Mapped[str] = mapped_column(String(60), index=True)
    summary: Mapped[str] = mapped_column(Text)
    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, native_enum=False, length=20), default=ActorType.system
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_runs.id"), index=True)
    subject_type: Mapped[str | None] = mapped_column(String(40))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"


class ApprovalRequest(PKMixin, TimestampMixin, Base):
    __tablename__ = "approval_requests"

    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_runs.id"), index=True)
    subject_type: Mapped[str] = mapped_column(String(40))  # thesis | order | artifact
    subject_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text, default="")
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    blocking: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, native_enum=False, length=20), default=ApprovalStatus.pending
    )
    resolved_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str] = mapped_column(Text, default="")
