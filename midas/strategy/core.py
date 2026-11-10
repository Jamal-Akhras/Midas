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

Two timing rules keep the live book on the backtested rule. The trend
signal is read from COMPLETED daily bars: the bar for the session in
progress is dropped, so the first pass of the month compares the prior
close with the average, not the opening print (the live quote sizes the
orders). And the month is stamped only once the plan has been executed or
proposed: an order that raises is retried on later passes, up to
MAX_REBALANCE_ATTEMPTS, while a broker rejection is this month's answer.
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
ATTEMPTS_KEY = "core_rebalance_attempts"
MAX_REBALANCE_ATTEMPTS = 3  # passes that may retry a rebalance whose orders raised
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
    signal_date: str = ""  # date of the last COMPLETED bar the trend rule read

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
            signal_date=str(content.get("signal_date") or ""),
        )


# ---------- pure rules ----------


def core_universe() -> list[str]:
    return [t.strip().upper() for t in settings.core_universe.split(",") if t.strip()]


def today_et() -> str:
    """Today's date in New York (ISO). A daily bar carrying this date is the
    session in progress, not a completed close."""
    now = utcnow()
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:  # no tz database: the UTC date only differs 19:00-24:00 ET
        return now.date().isoformat()


def completed_bars(
    bars: Sequence[dict[str, Any]], today: str | None = None
) -> list[dict[str, Any]]:
    """The bars the trend rule may read: a trailing bar dated today is the
    session in progress (Alpaca serves it intraday, partial volume and all)
    and is dropped, so the comparison uses the prior close as the backtest
    does. Only the last bar can be today's; earlier bars are left alone."""
    data = list(bars)
    if data and str(data[-1].get("date", ""))[:10] == (today or today_et()):
        data = data[:-1]
    return data


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
    """Once per calendar month: due until a pass has executed or proposed
    the month's plan (or given up after MAX_REBALANCE_ATTEMPTS)."""
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


def _record_attempt(db: Session) -> int:
    """Count a rebalance pass whose orders raised; the count is per calendar
    month so a transient outage on the first trading day is retried on the
    next passes, not every day until month-end."""
    month = utcnow().strftime("%Y-%m")
    row = db.get(SystemState, ATTEMPTS_KEY)
    value: dict[str, Any] = (
        dict(row.value) if row is not None and row.value.get("month") == month else {}
    )
    attempts = int(value.get("attempts", 0)) + 1
    value = {"month": month, "attempts": attempts, "at": utcnow().isoformat()}
    if row is None:
        db.add(SystemState(key=ATTEMPTS_KEY, value=value))
    else:
        row.value = value
    db.flush()
    return attempts


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
    today = today_et()
    closes: dict[str, list[float]] = {}
    prices: dict[str, float] = {}
    bar_dates: dict[str, str] = {}
    for ticker in universe:
        bars = execute(
            "get_daily_bars", {"ticker": ticker, "days": settings.core_trend_window + 10}
        )
        data = completed_bars(bars["data"], today)
        series = [float(b["close"]) for b in data]
        if not series:
            continue
        closes[ticker] = series
        bar_dates[ticker] = str(data[-1].get("date", ""))[:10]
        prices[ticker] = _live_price(execute, ticker, series[-1])  # sizing only
    targets, trend = trend_targets(closes, settings.core_trend_window)
    for ticker, bar_date in bar_dates.items():
        trend.setdefault(ticker, {})["signal_date"] = bar_date
    signal_date = max(bar_dates.values(), default="")
    applied = dict(vetoes) if vetoes is not None else active_vetoes(db, universe)
    for ticker, reason in applied.items():
        if ticker in targets:
            targets[ticker] = 0.0
            trend.setdefault(ticker, {})["veto"] = reason
    holdings: dict[str, float] = {}
    for p in core_positions(db):  # several rows for one ticker still add up
        holdings[p.ticker] = holdings.get(p.ticker, 0.0) + p.qty
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
        signal_date=signal_date,
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
        + (f"; vetoed: {', '.join(plan.vetoes)}" if plan.vetoes else "")
        + (f"; signal {plan.signal_date}" if plan.signal_date else ""),
        run_id=run.id,
        subject_type="artifact",
        subject_id=artifact.id,
        payload={
            "orders": len(plan.orders),
            "vetoes": plan.vetoes,
            "signal_date": plan.signal_date,
        },
    )
    result: dict[str, Any] = {
        "run_id": str(run.id),
        "artifact_id": str(artifact.id),
        "signal_date": plan.signal_date,
    }

    # The month is stamped only once this plan is the month's answer: nothing
    # to do, executed (rejections included), proposed at L0, or abandoned
    # after MAX_REBALANCE_ATTEMPTS passes whose orders raised.
    if not plan.orders:
        _mark_rebalanced(db, run.id)
        run.status = RunStatus.done
        return {**result, "status": "nothing_to_do"}
    if settings.autonomy_level >= 1:
        report = execute_plan(db, artifact.id, approved_by=f"core_book(L{settings.autonomy_level})")
        orders = [f"{o.side.value} {o.qty:g} {o.symbol}: {o.status.value}" for o in report.orders]
        if report.failed:
            attempts = _record_attempt(db)
            exhausted = attempts >= MAX_REBALANCE_ATTEMPTS
            if exhausted:
                _mark_rebalanced(db, run.id)
            run.status = RunStatus.failed
            run.error = (
                f"{len(report.failed)} order(s) raised (attempt {attempts}): "
                + "; ".join(f"{t}: {e}" for t, e in report.failed.items())
            )[:800]
            return {
                **result,
                "status": "failed",
                "orders": orders,
                "failed": report.failed,
                "rejected": report.rejected,
                "attempts": attempts,
                "retry": not exhausted,
            }
        _mark_rebalanced(db, run.id)
        run.status = RunStatus.done
        return {**result, "status": "executed", "orders": orders, "rejected": report.rejected}
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
    _mark_rebalanced(db, run.id)  # approved or denied, the request is this month's answer
    run.status = RunStatus.awaiting_approval
    return {**result, "status": "awaiting_approval"}


@dataclass
class ExecutionReport:
    """What execute_plan did: every order it created, the fills, the tickers
    whose orders the broker rejected (this month's answer) and the tickers
    whose orders raised (retried on a later pass)."""

    orders: list[Order] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


def core_holdings(db: Session) -> dict[str, list[Position]]:
    """Open core positions by ticker, oldest first. One row per ticker is
    the rule (a top-up adds to it); several rows are a legacy a sell must
    still cover in full."""
    out: dict[str, list[Position]] = {}
    for p in sorted(core_positions(db), key=lambda p: aware(p.opened_at)):
        out.setdefault(p.ticker, []).append(p)
    return out


def execute_plan(db: Session, artifact_id: uuid.UUID, *, approved_by: str) -> ExecutionReport:
    """Turn an approved plan into paper orders: sells first, then buys up to
    the allocation that is actually free. Every fill lands in the core book.
    An order that raises is recorded (`core.failed`) and the rest of the plan
    still runs; the caller decides whether the month is done."""
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
    report = ExecutionReport()
    for planned in plan.orders:
        side = OrderSide.buy if planned.side == "buy" else OrderSide.sell
        holdings = core_holdings(db)
        rows = holdings.get(planned.ticker, [])
        try:
            if side == OrderSide.sell:
                _sell_slice(db, artifact, planned, rows, allocation, approved_by, report)
            else:
                _buy_slice(db, artifact, planned, holdings, allocation, approved_by, report)
        except Exception as exc:  # noqa: BLE001 - one bad order must not abandon the plan
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            report.failed[planned.ticker] = error
            for order in report.orders:  # never leave a core order sitting 'approved'
                if order.symbol == planned.ticker and order.status == OrderStatus.approved:
                    order.status = OrderStatus.rejected
                    order.error = error
            logger.exception("core %s %s raised", planned.side, planned.ticker)
            record_event(
                db,
                event_type="core.failed",
                summary=f"{planned.ticker}: core {planned.side} raised {type(exc).__name__}; "
                "retried on a later pass",
                run_id=artifact.run_id,
                payload={"error": error, "side": planned.side, "qty": planned.qty},
            )
        thesis = _find_core_thesis(db, planned.ticker)
        if (
            side == OrderSide.buy
            and not rows
            and thesis is not None
            and thesis.status == ThesisStatus.approved
        ):
            # the slice's first buy did not fill: nothing may retry the thesis
            thesis.status = ThesisStatus.rejected
    artifact.content = {
        **artifact.content,
        "executed_at": utcnow().isoformat(),
        "fills": report.fills,
        "rejected": report.rejected,
        "failed": report.failed,
    }
    record_event(
        db,
        event_type="core.executed",
        summary=f"core rebalance: {len(report.fills)} of {len(report.orders)} order(s) filled"
        + (f"; rejected: {', '.join(report.rejected)}" if report.rejected else "")
        + (f"; raised: {', '.join(report.failed)}" if report.failed else ""),
        run_id=artifact.run_id,
        subject_type="artifact",
        subject_id=artifact.id,
        payload={"fills": report.fills, "rejected": report.rejected, "failed": report.failed},
    )
    db.flush()
    return report


def _sell_slice(
    db: Session,
    artifact: ResearchArtifact,
    planned: PlannedOrder,
    rows: Sequence[Position],
    allocation: float,
    approved_by: str,
    report: ExecutionReport,
) -> None:
    """Sell the planned quantity across every open row for the ticker,
    oldest first, so a trend exit covers the full held quantity."""
    remaining = planned.qty
    for position in rows:
        if remaining <= 1e-9:
            break
        qty = min(remaining, position.qty)
        if qty >= position.qty - 1e-9:  # rounding must not leave a dust row open
            qty = position.qty
        reason = OrderReason.exit if qty >= position.qty else OrderReason.trim
        _place(
            db,
            artifact,
            planned,
            OrderSide.sell,
            qty,
            position,
            reason,
            allocation,
            approved_by,
            report,
        )
        remaining -= qty


def _buy_slice(
    db: Session,
    artifact: ResearchArtifact,
    planned: PlannedOrder,
    holdings: Mapping[str, Sequence[Position]],
    allocation: float,
    approved_by: str,
    report: ExecutionReport,
) -> None:
    """Buy up to the allocation that is actually free; a ticker already held
    is topped up on its existing row, never given a second one."""
    deployed = sum(
        p.qty * (p.last_mark_price or p.avg_price) for rows in holdings.values() for p in rows
    )
    room = allocation * 1.01 - deployed  # 1% tolerance for marks moving since the plan
    qty = planned.qty
    if qty * planned.est_price > room:
        qty = max(room / planned.est_price, 0.0)
        if qty * planned.est_price < MIN_TRADE_USD:
            record_event(
                db,
                event_type="core.skipped",
                summary=f"{planned.ticker}: buy skipped, core allocation is full",
                run_id=artifact.run_id,
            )
            return
    rows = holdings.get(planned.ticker, [])
    position = rows[0] if rows else None
    _place(
        db,
        artifact,
        planned,
        OrderSide.buy,
        qty,
        position,
        OrderReason.entry,
        allocation,
        approved_by,
        report,
    )


def _place(
    db: Session,
    artifact: ResearchArtifact,
    planned: PlannedOrder,
    side: OrderSide,
    qty: float,
    position: Position | None,
    reason: OrderReason,
    allocation: float,
    approved_by: str,
    report: ExecutionReport,
) -> Order:
    """One core order: created, audited, submitted, and booked on a fill.
    `position` is the row a sell reduces or a buy tops up (None: a new row)."""
    from midas.execution import paper_broker

    thesis = _core_thesis(db, planned.ticker, artifact)
    order = Order(
        thesis_id=thesis.id,
        position_id=position.id if position is not None else None,
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
    report.orders.append(order)
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
        report.fills.append(
            {
                "ticker": planned.ticker,
                "side": side.value,
                "qty": order.qty,
                "price": order.filled_price,
            }
        )
    elif order.status == OrderStatus.rejected:
        report.rejected[planned.ticker] = order.error
        record_event(
            db,
            event_type="core.rejected",
            summary=f"{planned.ticker}: core {side.value} rejected by the broker: {order.error}",
            run_id=artifact.run_id,
            subject_type="order",
            subject_id=order.id,
            payload={"error": order.error, "side": side.value, "qty": order.qty},
        )
    return order


def _find_core_thesis(db: Session, ticker: str) -> TradeThesis | None:
    """The standing core thesis for a ticker, if one is live."""
    return db.scalar(
        select(TradeThesis)
        .where(
            TradeThesis.ticker == ticker,
            TradeThesis.book == CORE_BOOK,
            TradeThesis.status.in_((ThesisStatus.approved, ThesisStatus.executed)),
        )
        .order_by(TradeThesis.created_at.desc())
    )


def _core_thesis(db: Session, ticker: str, artifact: ResearchArtifact) -> TradeThesis:
    """One standing thesis per core ticker: the rule is the thesis."""
    existing = _find_core_thesis(db, ticker)
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
