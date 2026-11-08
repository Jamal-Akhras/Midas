"""The core book: a trend-filtered ETF allocation, rebalanced monthly.

Rules (the same ones midas.research.strategies.tactical_sma backtests):
each ticker in CORE_UNIVERSE gets an equal slice of CORE_ALLOCATION_PCT of
equity while its close is above its CORE_TREND_WINDOW-day average, and sits
in cash otherwise. A high-severity negative event flag on a ticker (see
midas.pipeline.events) zeroes its slice for that rebalance.

Positions live in the "core" book: the swing committee's stops, time stops,
reviews and risk caps leave them alone, and its statistics exclude them.
Execution goes through the autonomy ladder like everything else: L0 raises
a blocking approval request carrying the whole plan, L1+ executes on paper.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from midas.audit import record_event
from midas.config import settings
from midas.models import (
    ActorType,
    ApprovalRequest,
    ArtifactType,
    Direction,
    EventFlag,
    Instrument,
    Order,
    OrderReason,
    OrderSide,
    OrderStatus,
    PipelineRun,
    Position,
    PositionStatus,
    ResearchArtifact,
    RunKind,
    RunStatus,
    SystemState,
    ThesisStatus,
    TradeThesis,
    aware,
    utcnow,
)

logger = logging.getLogger(__name__)

CORE_BOOK = "core"
SWING_BOOK = "swing"
LAST_REBALANCE_KEY = "core_last_rebalance"
MIN_TRADE_USD = 25.0


@dataclass(frozen=True)
class PlannedOrder:
    ticker: str
    side: str  # buy | sell
    qty: float
    est_price: float
    target_weight: float
    current_weight: float
    note: str = ""

    @property
    def est_notional(self) -> float:
        return self.qty * self.est_price


@dataclass
class CorePlan:
    as_of: str
    equity: float
    allocation_usd: float
    targets: dict[str, float]
    vetoes: dict[str, str]
    trend: dict[str, dict[str, Any]]
    orders: list[PlannedOrder] = field(default_factory=list)

    def to_content(self) -> dict[str, Any]:
        content = asdict(self)
        content["orders"] = [asdict(o) for o in self.orders]
        content["rules"] = (
            f"equal slices of {settings.core_allocation_pct:g}% of equity across "
            f"{len(self.targets)} tickers, each held only above its "
            f"{settings.core_trend_window}-day average; high-severity negative event "
            f"flags within {settings.core_veto_days} days veto a slice"
        )
        return content

    @classmethod
    def from_content(cls, content: Mapping[str, Any]) -> CorePlan:
        orders = [PlannedOrder(**o) for o in content.get("orders", [])]
        return cls(
            as_of=str(content["as_of"]),
            equity=float(content["equity"]),
            allocation_usd=float(content["allocation_usd"]),
            targets={str(k): float(v) for k, v in content["targets"].items()},
            vetoes={str(k): str(v) for k, v in content.get("vetoes", {}).items()},
            trend=dict(content.get("trend", {})),
            orders=orders,
        )


# ---------- pure rules ----------


def core_universe() -> list[str]:
    return [t.strip().upper() for t in settings.core_universe.split(",") if t.strip()]


def trend_targets(
    closes: Mapping[str, Sequence[float]], window: int
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """Equal slices, each held only while the last close is above the
    `window`-day simple moving average. Tickers without enough history get
    no slice (cash), and the detail says why."""
    if not closes:
        return {}, {}
    slice_w = 1.0 / len(closes)
    targets: dict[str, float] = {}
    detail: dict[str, dict[str, Any]] = {}
    for ticker, series in closes.items():
        values = [float(v) for v in series if v is not None]
        if len(values) < window:
            targets[ticker] = 0.0
            detail[ticker] = {"bars": len(values), "note": f"fewer than {window} bars"}
            continue
        sma = sum(values[-window:]) / window
        last = values[-1]
        above = last > sma
        targets[ticker] = slice_w if above else 0.0
        detail[ticker] = {"close": round(last, 4), "sma": round(sma, 4), "above": above}
    return targets, detail


def plan_orders(
    *,
    equity: float,
    allocation_pct: float,
    targets: Mapping[str, float],
    prices: Mapping[str, float],
    holdings: Mapping[str, float],
    fractional: bool = True,
    min_trade_usd: float = MIN_TRADE_USD,
) -> list[PlannedOrder]:
    """Orders that move the book from `holdings` (ticker -> qty) to the
    target weights of `allocation_pct` of equity. Sells come first so they
    fund the buys; trades below `min_trade_usd` are skipped."""
    allocation = equity * allocation_pct / 100.0
    orders: list[PlannedOrder] = []
    for ticker in sorted(set(targets) | set(holdings)):
        price = float(prices.get(ticker, 0.0))
        if price <= 0:
            continue
        target_usd = allocation * float(targets.get(ticker, 0.0))
        current_qty = float(holdings.get(ticker, 0.0))
        current_usd = current_qty * price
        diff = target_usd - current_usd
        if abs(diff) < min_trade_usd:
            continue
        qty = abs(diff) / price
        if diff < 0:
            qty = min(qty, current_qty)
        if not fractional:
            qty = float(int(qty))
        if qty <= 0 or qty * price < min_trade_usd:
            continue
        orders.append(
            PlannedOrder(
                ticker=ticker,
                side="buy" if diff > 0 else "sell",
                qty=round(qty, 6),
                est_price=price,
                target_weight=float(targets.get(ticker, 0.0)),
                current_weight=(current_usd / allocation) if allocation else 0.0,
            )
        )
    orders.sort(key=lambda o: (o.side != "sell", o.ticker))
    return orders


# ---------- database views ----------


def core_positions(db: Session) -> list[Position]:
    return list(
        db.scalars(
            select(Position).where(
                Position.status == PositionStatus.open, Position.book == CORE_BOOK
            )
        )
    )


def active_vetoes(db: Session, tickers: Sequence[str], days: int | None = None) -> dict[str, str]:
    """Tickers with a high-severity negative event flag inside the window."""
    window = timedelta(days=days if days is not None else settings.core_veto_days)
    since = utcnow() - window
    out: dict[str, str] = {}
    rows = db.scalars(
        select(EventFlag)
        .where(
            EventFlag.ticker.in_(list(tickers)),
            EventFlag.direction == "negative",
            EventFlag.severity == "high",
        )
        .order_by(EventFlag.created_at.desc())
    )
    for flag in rows:
        if aware(flag.created_at) < since or flag.ticker in out:
            continue
        out[flag.ticker] = f"{flag.kind}: {flag.summary[:100]}"
    return out


def rebalance_due(db: Session) -> bool:
    """Once per calendar month, on the first pass that gets this far."""
    row = db.get(SystemState, LAST_REBALANCE_KEY)
    last = str(row.value.get("at", "")) if row else ""
    return last[:7] != utcnow().strftime("%Y-%m")


def _mark_rebalanced(db: Session, run_id: uuid.UUID) -> None:
    row = db.get(SystemState, LAST_REBALANCE_KEY)
    value = {"at": utcnow().isoformat(), "run_id": str(run_id)}
    if row is None:
        db.add(SystemState(key=LAST_REBALANCE_KEY, value=value))
    else:
        row.value = value
    db.flush()


def _live_price(execute: Any, ticker: str, fallback: float) -> float:
    """Latest trade for sizing; the last bar close when no quote source answers."""
    try:
        return float(execute("get_quote", {"ticker": ticker})["data"]["price"])
    except Exception:
        return fallback


def build_plan(db: Session, *, vetoes: Mapping[str, str] | None = None) -> CorePlan:
    """Read bars for the universe, apply the trend rule and the vetoes, and
    diff against the current core holdings. Pure data in, plan out; the
    bars are snapshotted by the tool layer like any other market data."""
    from midas.execution.paper_broker import get_account
    from midas.tools.base import ToolContext
    from midas.tools.registry import TOOLS

    universe = core_universe()
    execute = ToolContext(db).executor(TOOLS)
    closes: dict[str, list[float]] = {}
    prices: dict[str, float] = {}
    for ticker in universe:
        bars = execute(
            "get_daily_bars", {"ticker": ticker, "days": settings.core_trend_window + 10}
        )
        series = [float(b["close"]) for b in bars["data"]]
        if not series:
            continue
        closes[ticker] = series
        prices[ticker] = _live_price(execute, ticker, series[-1])
    targets, trend = trend_targets(closes, settings.core_trend_window)
    applied = dict(vetoes) if vetoes is not None else active_vetoes(db, universe)
    for ticker, reason in applied.items():
        if ticker in targets:
            targets[ticker] = 0.0
            trend.setdefault(ticker, {})["veto"] = reason
    holdings = {p.ticker: p.qty for p in core_positions(db)}
    for ticker in holdings:
        if ticker not in prices:  # a ticker dropped from the universe: still needs a price to exit
            bars = execute("get_daily_bars", {"ticker": ticker, "days": 5})
            if bars["data"]:
                prices[ticker] = _live_price(execute, ticker, float(bars["data"][-1]["close"]))
    equity = float(get_account(db)["equity"])
    orders = plan_orders(
        equity=equity,
        allocation_pct=settings.core_allocation_pct,
        targets=targets,
        prices=prices,
        holdings=holdings,
        fractional=settings.fractional_shares,
    )
    return CorePlan(
        as_of=utcnow().isoformat(),
        equity=round(equity, 2),
        allocation_usd=round(equity * settings.core_allocation_pct / 100.0, 2),
        targets=targets,
        vetoes=applied,
        trend=trend,
        orders=orders,
    )


# ---------- the ladder ----------


def run_rebalance(db: Session, *, force: bool = False) -> dict[str, Any]:
    """Plan this month's rebalance and route it through the autonomy
    ladder. The caller owns the transaction. `force` ignores the monthly
    cadence and the CORE_ENABLED switch (operator-driven runs)."""
    from midas.risk.engine import get_halt

    if not settings.core_enabled and not force:
        return {"status": "disabled"}
    if not force and not rebalance_due(db):
        return {"status": "not_due"}
    halt = get_halt(db)
    if halt:
        return {"status": "halted", "reason": halt["reason"]}

    run = PipelineRun(kind=RunKind.core_rebalance, ticker="")
    db.add(run)
    db.flush()
    record_event(
        db,
        event_type="run.created",
        summary="Core rebalance run created",
        actor_type=ActorType.operator if force else ActorType.system,
        run_id=run.id,
    )
    plan = build_plan(db)
    artifact = ResearchArtifact(
        run_id=run.id,
        ticker="",
        agent_slug="core-book",
        artifact_type=ArtifactType.core_plan,
        content=plan.to_content(),
        version=1,
        model_used="deterministic-code",
        cost_usd=0.0,
    )
    db.add(artifact)
    db.flush()
    held = ", ".join(f"{t} {w:.0%}" for t, w in plan.targets.items() if w > 0) or "all cash"
    record_event(
        db,
        event_type="core.planned",
        summary=f"core plan: {len(plan.orders)} order(s); targets: {held}"
        + (f"; vetoed: {', '.join(plan.vetoes)}" if plan.vetoes else ""),
        run_id=run.id,
        subject_type="artifact",
        subject_id=artifact.id,
        payload={"orders": len(plan.orders), "vetoes": plan.vetoes},
    )
    _mark_rebalanced(db, run.id)  # a denied plan is this month's answer; no daily retries
    result: dict[str, Any] = {"run_id": str(run.id), "artifact_id": str(artifact.id)}

    if not plan.orders:
        run.status = RunStatus.done
        return {**result, "status": "nothing_to_do"}
    if settings.autonomy_level >= 1:
        orders = execute_plan(db, artifact.id, approved_by=f"core_book(L{settings.autonomy_level})")
        run.status = RunStatus.done
        return {
            **result,
            "status": "executed",
            "orders": [f"{o.side.value} {o.qty:g} {o.symbol}: {o.status.value}" for o in orders],
        }
    db.add(
        ApprovalRequest(
            run_id=run.id,
            subject_type="core_plan",
            subject_id=artifact.id,
            title=f"Core rebalance: {len(plan.orders)} order(s) ({held})",
            summary="; ".join(
                f"{o.side} {o.qty:g} {o.ticker} @ ~{o.est_price:.2f}" for o in plan.orders
            ),
            context=plan.to_content(),
            blocking=True,
        )
    )
    record_event(
        db,
        event_type="approval.requested",
        summary="core rebalance awaits operator approval (L0)",
        run_id=run.id,
        subject_type="artifact",
        subject_id=artifact.id,
    )
    run.status = RunStatus.awaiting_approval
    return {**result, "status": "awaiting_approval"}


def execute_plan(db: Session, artifact_id: uuid.UUID, *, approved_by: str) -> list[Order]:
    """Turn an approved plan into paper orders: sells first, then buys up to
    the allocation that is actually free. Every fill lands in the core book."""
    from midas.execution import paper_broker
    from midas.risk.engine import get_halt

    artifact = db.get(ResearchArtifact, artifact_id)
    if artifact is None or artifact.artifact_type != ArtifactType.core_plan:
        raise ValueError(f"{artifact_id} is not a core plan")
    halt = get_halt(db)
    if halt:
        raise RuntimeError(f"pipeline halted: {halt['reason']}")
    plan = CorePlan.from_content(artifact.content)
    equity = float(paper_broker.get_account(db)["equity"])
    allocation = equity * settings.core_allocation_pct / 100.0
    orders: list[Order] = []
    fills: list[dict[str, Any]] = []
    for planned in plan.orders:
        held = {p.ticker: p for p in core_positions(db)}
        position = held.get(planned.ticker)
        side = OrderSide.buy if planned.side == "buy" else OrderSide.sell
        qty = planned.qty
        if side == OrderSide.sell:
            if position is None:
                continue
            qty = min(qty, position.qty)
            reason = OrderReason.exit if qty >= position.qty else OrderReason.trim
        else:
            deployed = sum(p.qty * (p.last_mark_price or p.avg_price) for p in held.values())
            room = allocation * 1.01 - deployed  # 1% tolerance for marks moving since the plan
            if qty * planned.est_price > room:
                qty = max(room / planned.est_price, 0.0)
                if qty * planned.est_price < MIN_TRADE_USD:
                    record_event(
                        db,
                        event_type="core.skipped",
                        summary=f"{planned.ticker}: buy skipped, core allocation is full",
                        run_id=artifact.run_id,
                    )
                    continue
            reason = OrderReason.entry
        thesis = _core_thesis(db, planned.ticker, artifact)
        order = Order(
            thesis_id=thesis.id,
            position_id=position.id if side == OrderSide.sell and position else None,
            side=side,
            instrument=Instrument.shares,
            symbol=planned.ticker,
            qty=round(qty, 6),
            reason=reason,
            approved_by=approved_by,
            status=OrderStatus.approved,
            risk_check={
                "book": CORE_BOOK,
                "allocation_usd": round(allocation, 2),
                "target_weight": planned.target_weight,
            },
        )
        db.add(order)
        db.flush()
        record_event(
            db,
            event_type="order.submitted",
            summary=f"core {side.value} {order.qty:g} {planned.ticker} ({approved_by})",
            run_id=artifact.run_id,
            subject_type="order",
            subject_id=order.id,
        )
        paper_broker.submit(db, order)
        if order.status == OrderStatus.filled:
            pos = paper_broker.apply_fill_to_position(db, order, thesis)
            pos.book = CORE_BOOK
            thesis.status = ThesisStatus.executed
            fills.append(
                {
                    "ticker": planned.ticker,
                    "side": side.value,
                    "qty": order.qty,
                    "price": order.filled_price,
                }
            )
        orders.append(order)
    artifact.content = {**artifact.content, "executed_at": utcnow().isoformat(), "fills": fills}
    record_event(
        db,
        event_type="core.executed",
        summary=f"core rebalance: {len(fills)} of {len(orders)} order(s) filled",
        run_id=artifact.run_id,
        subject_type="artifact",
        subject_id=artifact.id,
        payload={"fills": fills},
    )
    db.flush()
    return orders


def _core_thesis(db: Session, ticker: str, artifact: ResearchArtifact) -> TradeThesis:
    """One standing thesis per core ticker: the rule is the thesis."""
    existing = db.scalar(
        select(TradeThesis)
        .where(
            TradeThesis.ticker == ticker,
            TradeThesis.book == CORE_BOOK,
            TradeThesis.status.in_((ThesisStatus.approved, ThesisStatus.executed)),
        )
        .order_by(TradeThesis.created_at.desc())
    )
    if existing is not None:
        return existing
    thesis = TradeThesis(
        run_id=artifact.run_id,
        verdict_artifact_id=artifact.id,
        ticker=ticker,
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=0.0,
        entry_zone_high=0.0,
        stop_price=0.0,  # no price stop: the trend filter is the exit, monthly
        targets=[],
        thesis_summary=(
            "Core book: equal slice while above the trend average; "
            "rules-based, rebalanced monthly, vetoed by high-severity event flags"
        ),
        invalidators=[
            "close below the trend average at a month-end",
            "high-severity negative event flag",
        ],
        review_schedule={"cadence_days": 0, "event_triggers": []},
        status=ThesisStatus.approved,
        book=CORE_BOOK,
    )
    db.add(thesis)
    db.flush()
    return thesis
