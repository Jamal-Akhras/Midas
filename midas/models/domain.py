"""Trading domain models.

Provenance is schema-enforced where possible: artifacts belong to a run,
snapshots link to the artifact that consumed them, theses link to their run,
orders link to their thesis, outcomes link position+thesis. "Why did we
enter/exit this" must be answerable months later from these tables alone.
"""
import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from midas.models.base import Base, PKMixin, TimestampMixin


def _enum(e):  # store enums as short strings, portable across sqlite/postgres
    return Enum(e, native_enum=False, length=32)


class RunKind(str, enum.Enum):
    screen = "screen"
    committee = "committee"
    reevaluation = "reevaluation"
    portfolio_review = "portfolio_review"
    post_mortem = "post_mortem"


class RunStatus(str, enum.Enum):
    running = "running"
    awaiting_approval = "awaiting_approval"
    done = "done"
    failed = "failed"
    halted = "halted"
    cancelled = "cancelled"


class PipelineRun(PKMixin, TimestampMixin, Base):
    """One execution of a pipeline: a universe screen, a per-ticker committee
    run, or a position re-evaluation. Stage tracking + artifacts give
    crash-resume: completed stages are skipped on retry."""

    __tablename__ = "pipeline_runs"

    kind: Mapped[RunKind] = mapped_column(_enum(RunKind))
    ticker: Mapped[str] = mapped_column(String(12), default="", index=True)
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id"))
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus), default=RunStatus.running)
    stage: Mapped[str] = mapped_column(String(40), default="")  # last stage started
    error: Mapped[str] = mapped_column(Text, default="")


class ArtifactType(str, enum.Enum):
    screen_results = "screen_results"
    fundamentals = "fundamentals"
    dossier = "dossier"
    catalysts = "catalysts"
    triage = "triage"
    bull_case = "bull_case"
    bear_case = "bear_case"
    structure = "structure"
    risk_check = "risk_check"
    verdict = "verdict"
    reevaluation = "reevaluation"
    portfolio_review = "portfolio_review"
    post_mortem = "post_mortem"


class ReviewStatus(str, enum.Enum):
    unreviewed = "unreviewed"
    pending_approval = "pending_approval"
    approved = "approved"
    rejected = "rejected"


class ResearchArtifact(PKMixin, TimestampMixin, Base):
    """Immutable agent output with full provenance. Orphans are
    schema-impossible: run_id is NOT NULL."""

    __tablename__ = "research_artifacts"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(12), default="", index=True)
    agent_slug: Mapped[str] = mapped_column(String(50))
    artifact_type: Mapped[ArtifactType] = mapped_column(_enum(ArtifactType), index=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    review_status: Mapped[ReviewStatus] = mapped_column(
        _enum(ReviewStatus), default=ReviewStatus.unreviewed
    )
    model_used: Mapped[str] = mapped_column(String(100), default="")
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class DataSnapshot(PKMixin, TimestampMixin, Base):
    """Every tool result an agent saw, verbatim. The audit trail must show
    the data the decision was based on."""

    __tablename__ = "data_snapshots"

    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_runs.id"), index=True)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_artifacts.id"), index=True
    )  # stamped once the consuming artifact exists
    ticker: Mapped[str] = mapped_column(String(12), default="", index=True)
    tool_name: Mapped[str] = mapped_column(String(60), index=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data: Mapped[Any] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(30))  # alpaca|yfinance|edgar|finnhub|fmp|brave|tavily|web|demo
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")


class Direction(str, enum.Enum):
    bullish = "bullish"
    bearish = "bearish"


class Instrument(str, enum.Enum):
    shares = "shares"
    option = "option"


class ThesisStatus(str, enum.Enum):
    proposed = "proposed"
    approved = "approved"
    rejected = "rejected"
    executed = "executed"
    closed = "closed"
    cancelled = "cancelled"


class TradeThesis(PKMixin, TimestampMixin, Base):
    __tablename__ = "trade_theses"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False)
    verdict_artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_artifacts.id"), nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    direction: Mapped[Direction] = mapped_column(_enum(Direction))
    instrument: Mapped[Instrument] = mapped_column(_enum(Instrument))
    entry_zone_low: Mapped[float] = mapped_column(Float)
    entry_zone_high: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float] = mapped_column(Float)          # invalidation level on the underlying
    targets: Mapped[list[Any]] = mapped_column(JSON, default=list)
    thesis_summary: Mapped[str] = mapped_column(Text, default="")
    invalidators: Mapped[list[Any]] = mapped_column(JSON, default=list)
    review_schedule: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[ThesisStatus] = mapped_column(_enum(ThesisStatus), default=ThesisStatus.proposed)


class OptionType(str, enum.Enum):
    call = "call"
    put = "put"


class OptionStrategy(str, enum.Enum):
    long_call = "long_call"
    long_put = "long_put"
    call_debit_spread = "call_debit_spread"
    put_debit_spread = "put_debit_spread"


class OptionStructure(PKMixin, TimestampMixin, Base):
    """A concrete, code-checked options structure for a thesis.
    Defined-risk only: long options or debit spreads; max loss = premium."""

    __tablename__ = "option_structures"

    thesis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trade_theses.id"), nullable=False)
    underlying: Mapped[str] = mapped_column(String(12))
    strategy: Mapped[OptionStrategy] = mapped_column(_enum(OptionStrategy))
    option_type: Mapped[OptionType] = mapped_column(_enum(OptionType))
    expiry: Mapped[date] = mapped_column(Date)
    dte_at_entry: Mapped[int] = mapped_column(Integer)
    long_strike: Mapped[float] = mapped_column(Float)
    short_strike: Mapped[float | None] = mapped_column(Float)  # spreads only
    est_premium_per_contract: Mapped[float] = mapped_column(Float)  # debit, per contract (x100)
    contracts: Mapped[int] = mapped_column(Integer, default=0)      # set by risk sizing
    liquidity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # named check -> pass/fail + value


class Broker(str, enum.Enum):
    paper_sim = "paper_sim"      # built-in simulator
    alpaca_paper = "alpaca_paper"
    # There is deliberately no real-money broker value. Adding one is the
    # L3 gate and requires the statistical bar in config.L3_BAR.


class OrderSide(str, enum.Enum):
    buy = "buy"
    sell = "sell"


class OrderStatus(str, enum.Enum):
    proposed = "proposed"
    approved = "approved"
    submitted = "submitted"
    filled = "filled"
    rejected = "rejected"
    cancelled = "cancelled"


class OrderReason(str, enum.Enum):
    entry = "entry"
    exit = "exit"
    trim = "trim"
    roll = "roll"


class Order(PKMixin, TimestampMixin, Base):
    __tablename__ = "orders"

    thesis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trade_theses.id"), nullable=False)
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id"))
    structure_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("option_structures.id"))
    broker: Mapped[Broker] = mapped_column(_enum(Broker), default=Broker.paper_sim)
    side: Mapped[OrderSide] = mapped_column(_enum(OrderSide))
    instrument: Mapped[Instrument] = mapped_column(_enum(Instrument))
    symbol: Mapped[str] = mapped_column(String(32))  # ticker or OCC option symbol
    qty: Mapped[float] = mapped_column(Float)        # shares or contracts
    limit_price: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[OrderReason] = mapped_column(_enum(OrderReason))
    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus), default=OrderStatus.proposed)
    approved_by: Mapped[str] = mapped_column(String(30), default="")  # "operator" | "investor_agent(L1/L2)"
    filled_price: Mapped[float | None] = mapped_column(Float)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    risk_check: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # execution-time re-check record
    alpaca_order_id: Mapped[str] = mapped_column(String(64), default="")
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")


class PositionStatus(str, enum.Enum):
    open = "open"
    closed = "closed"


class Position(PKMixin, TimestampMixin, Base):
    __tablename__ = "positions"

    thesis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trade_theses.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    symbol: Mapped[str] = mapped_column(String(32))  # ticker or OCC option symbol
    instrument: Mapped[Instrument] = mapped_column(_enum(Instrument))
    sector: Mapped[str] = mapped_column(String(50), default="")
    qty: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float] = mapped_column(Float, default=0.0)
    option_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # expiry/strike/type for options
    status: Mapped[PositionStatus] = mapped_column(_enum(PositionStatus), default=PositionStatus.open)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    last_mark_price: Mapped[float | None] = mapped_column(Float)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


# NOTE: Order.alpaca_order_id is added via lightweight migration in
# midas.db.init_db for pre-existing databases.


class ScreenCandidate(PKMixin, TimestampMixin, Base):
    """Counterfactual ledger: EVERY screened candidate, whether or not it
    got a committee. Forward returns filled in later - so 'does the
    committee beat the screener?' has data instead of vibes."""
    __tablename__ = "screen_candidates"

    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_runs.id"))
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    price_at_screen: Mapped[float] = mapped_column(Float, default=0.0)
    fwd_10d_pct: Mapped[float | None] = mapped_column(Float)
    fwd_20d_pct: Mapped[float | None] = mapped_column(Float)


class StandingStop(PKMixin, TimestampMixin, Base):
    """A GTC stop-loss order resting at the REAL Alpaca paper account so
    the stop holds even when the monitor process is down. One per open
    shares position; ratchets replace it when the effective stop moves."""
    __tablename__ = "standing_stops"

    position_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("positions.id"), nullable=False, index=True)
    alpaca_order_id: Mapped[str] = mapped_column(String(64), default="")
    stop_price: Mapped[float] = mapped_column(Float, default=0.0)
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[bool] = mapped_column(default=True)


class CheckpointTrigger(str, enum.Enum):
    scheduled = "scheduled"
    earnings = "earnings"
    invalidation = "invalidation"
    dte = "dte"
    manual = "manual"
    time_stop = "time_stop"


class CheckpointStatus(str, enum.Enum):
    pending = "pending"
    done = "done"
    skipped = "skipped"


class ReviewCheckpoint(PKMixin, TimestampMixin, Base):
    __tablename__ = "review_checkpoints"

    position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("positions.id"), nullable=False, index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[CheckpointTrigger] = mapped_column(_enum(CheckpointTrigger))
    status: Mapped[CheckpointStatus] = mapped_column(_enum(CheckpointStatus), default=CheckpointStatus.pending)
    decision: Mapped[str] = mapped_column(String(20), default="")  # hold|trim|exit|roll
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("research_artifacts.id"))
    notes: Mapped[str] = mapped_column(Text, default="")


class TradeOutcome(PKMixin, TimestampMixin, Base):
    """The eval harness: every closed position scored against its thesis.
    Graded by the market, not by a model."""

    __tablename__ = "trade_outcomes"

    position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("positions.id"), nullable=False, unique=True)
    thesis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trade_theses.id"), nullable=False)
    pnl_usd: Mapped[float] = mapped_column(Float)
    pnl_pct: Mapped[float] = mapped_column(Float)
    holding_days: Mapped[int] = mapped_column(Integer)
    exit_kind: Mapped[str] = mapped_column(String(20))  # target|stop|time|manual|expiry
    bull_case_right: Mapped[bool | None] = mapped_column(Boolean)
    bear_case_right: Mapped[bool | None] = mapped_column(Boolean)
    invalidation_worked: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str] = mapped_column(Text, default="")


class SystemState(Base):
    """Key-value operational state (halt flags, paper account, marks)."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
