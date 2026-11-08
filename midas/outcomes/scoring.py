"""Outcome tracking: the eval harness. Every closed position is scored
against its thesis - P&L, which case proved right, whether invalidation
worked as designed. Graded by the market, not by a model."""

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from midas.audit import record_event
from midas.config import L3_BAR, settings
from midas.models import (
    ArtifactType,
    Direction,
    Instrument,
    Position,
    PositionStatus,
    ResearchArtifact,
    TradeOutcome,
    TradeThesis,
    aware,
    utcnow,
)

logger = logging.getLogger(__name__)


def score_closed_positions(db: Session) -> list[TradeOutcome]:
    """Create TradeOutcome rows for closed positions that lack one."""
    scored: list[TradeOutcome] = []
    closed = db.scalars(select(Position).where(Position.status == PositionStatus.closed)).all()
    have = {o.position_id for o in db.scalars(select(TradeOutcome))}
    for p in closed:
        if p.id in have:
            continue
        thesis = db.get(TradeThesis, p.thesis_id)
        assert thesis is not None, f"closed position {p.id} has no thesis"
        outcome = _score(db, p, thesis)
        db.add(outcome)
        db.flush()
        thesis.status = thesis.status  # closed theses keep their status history in audit
        record_event(
            db,
            event_type="outcome.scored",
            summary=f"{p.symbol}: {outcome.pnl_usd:+.2f} USD "
            f"({outcome.exit_kind}); bull_right={outcome.bull_case_right}",
            subject_type="outcome",
            subject_id=outcome.id,
            payload={"pnl_usd": outcome.pnl_usd, "pnl_pct": outcome.pnl_pct},
        )
        scored.append(outcome)
    return scored


def _score(db: Session, p: Position, thesis: TradeThesis) -> TradeOutcome:
    multiplier = 100 if p.instrument == Instrument.option else 1
    cost_basis = p.avg_price * multiplier  # per unit
    pnl = p.realized_pnl_usd
    invested = cost_basis * _initial_qty(db, p)
    pnl_pct = (pnl / invested * 100) if invested else 0.0
    closed_at = p.closed_at or utcnow()  # scored positions are closed; defensive
    holding_days = max((aware(closed_at) - aware(p.opened_at)).days, 0)

    exit_kind = "manual"
    last_mark = p.last_mark_price or p.avg_price
    stop = p.stop_price or thesis.stop_price
    if p.instrument == Instrument.shares:
        if thesis.direction == Direction.bullish and last_mark <= stop:
            exit_kind = "stop"
        elif thesis.targets and last_mark >= min(thesis.targets):
            exit_kind = "target"
    # was the directional call right, judged by realized P&L direction?
    bull_right = pnl > 0 if thesis.direction == Direction.bullish else pnl < 0
    bear_art = _latest(db, thesis.ticker, ArtifactType.bear_case)
    bull_art = _latest(db, thesis.ticker, ArtifactType.bull_case)
    invalidation_worked = None
    if exit_kind == "stop":
        # stop honored: loss should be near the planned per-trade risk cap
        invalidation_worked = abs(pnl) <= invested * 1.5 if invested else None

    return TradeOutcome(
        position_id=p.id,
        thesis_id=thesis.id,
        pnl_usd=round(pnl, 2),
        pnl_pct=round(pnl_pct, 2),
        holding_days=holding_days,
        exit_kind=exit_kind,
        bull_case_right=bull_right if bull_art else None,
        bear_case_right=(not bull_right) if bear_art else None,
        invalidation_worked=invalidation_worked,
        notes=f"thesis: {thesis.thesis_summary[:200]}",
    )


def _initial_qty(db: Session, p: Position) -> float:
    from midas.models import Order, OrderSide, OrderStatus

    buys = db.scalars(
        select(Order).where(
            Order.position_id == p.id,
            Order.side == OrderSide.buy,
            Order.status == OrderStatus.filled,
        )
    ).all()
    return sum(o.qty for o in buys) or p.qty or 1.0


def _latest(db: Session, ticker: str, at: ArtifactType) -> ResearchArtifact | None:
    return db.scalar(
        select(ResearchArtifact)
        .where(ResearchArtifact.ticker == ticker, ResearchArtifact.artifact_type == at)
        .order_by(ResearchArtifact.created_at.desc())
    )


def stats(db: Session) -> dict:
    """Track record vs the L3 bar. `midas stats` renders this. The L3
    cohort counts ONLY broker-backed shares outcomes: simulator-only option
    fills are reported separately and never feed promotion evidence."""
    import random as _random

    from midas.models import Broker, Order, SystemState

    pos_book = {p.id: p.book for p in db.scalars(select(Position))}
    # the core book is rules-based and measured in docs/results.md; its fills
    # are not evidence about the committee
    outcomes = [
        o
        for o in db.scalars(select(TradeOutcome))
        if pos_book.get(o.position_id, "swing") == "swing"
    ]
    n = len(outcomes)
    wins = [o for o in outcomes if o.pnl_usd > 0]
    total = sum(o.pnl_usd for o in outcomes)
    expectancy = total / n if n else 0.0

    # eligible cohort: outcomes from the CURRENT L2 evidence period only
    # (the fingerprint clock restarting must orphan prior-strategy trades),
    # from shares positions with a complete broker round trip: at least one
    # broker-filled entry, broker-filled exits covering the full quantity,
    # and no simulated fill anywhere in the lifecycle
    from datetime import datetime as _dt

    from midas.models import OrderSide, OrderStatus

    l2row = db.get(SystemState, "l2_since")
    l2_start = (
        aware(_dt.fromisoformat(l2row.value["at"])) if l2row and l2row.value.get("at") else None
    )

    brokers_by_pos: dict = {}
    buy_qty: dict = {}
    sell_qty: dict = {}
    for od in db.scalars(
        select(Order).where(Order.status == OrderStatus.filled, Order.position_id.isnot(None))
    ):
        brokers_by_pos.setdefault(od.position_id, set()).add(od.broker)
        bucket = buy_qty if od.side == OrderSide.buy else sell_qty
        bucket[od.position_id] = bucket.get(od.position_id, 0.0) + od.qty
    pos_instrument = {p.id: p.instrument for p in db.scalars(select(Position))}
    eligible = [
        o
        for o in outcomes
        if l2_start is not None
        and aware(o.created_at) >= l2_start
        and pos_instrument.get(o.position_id) == Instrument.shares
        and brokers_by_pos.get(o.position_id) == {Broker.alpaca_paper}
        and buy_qty.get(o.position_id, 0.0) > 0
        and sell_qty.get(o.position_id, 0.0) >= buy_qty.get(o.position_id, 0.0)
    ]
    n_el = len(eligible)
    total_el = sum(o.pnl_usd for o in eligible)
    exp_el = total_el / n_el if n_el else 0.0
    win_sum = sum(o.pnl_usd for o in eligible if o.pnl_usd > 0)
    loss_sum = abs(sum(o.pnl_usd for o in eligible if o.pnl_usd < 0))
    profit_factor = round(win_sum / loss_sum, 2) if loss_sum else None
    p_pos = None
    if n_el >= 5:  # bootstrap: probability the cohort expectancy is > 0
        pnls = [o.pnl_usd for o in eligible]
        rng = _random.Random(42)
        hits = sum(1 for _ in range(2000) if sum(rng.choices(pnls, k=n_el)) > 0)
        p_pos = round(hits / 2000, 3)

    days_at_l2 = (utcnow() - l2_start).days if l2_start is not None else None

    # max drawdown: separate curves - all trades vs the eligible cohort
    # (the L3 gate must judge the cohort it counts, nothing else)
    def _max_dd(rows):
        curve, peak, dd = 0.0, 0.0, 0.0
        for o in sorted(rows, key=lambda o: aware(o.created_at)):
            curve += o.pnl_usd
            peak = max(peak, curve)
            dd = max(dd, peak - curve)
        return dd

    max_dd = _max_dd(outcomes)
    max_dd_el = _max_dd(eligible)
    from midas.execution.paper_broker import starting_cash

    STARTING_CASH = starting_cash()
    from midas.models import CostEvent

    # Self-funding: does trailing-30d realized P&L cover trailing-30d AI
    # spend? Paper P&L does not pay real bills - this is the evidence
    # metric for whether it EVER could.
    cutoff = utcnow() - timedelta(days=30)
    pnl_30d = sum(o.pnl_usd for o in outcomes if aware(o.created_at) >= cutoff)
    spend_30d = sum(
        e.cost_usd for e in db.scalars(select(CostEvent)) if aware(e.created_at) >= cutoff
    )
    from midas.models import ScreenCandidate
    from midas.models.domain import ThesisStatus

    cands = db.scalars(select(ScreenCandidate).where(ScreenCandidate.fwd_10d_pct.isnot(None))).all()
    traded_tickers = {
        t.ticker
        for t in db.scalars(
            select(TradeThesis).where(TradeThesis.status.in_((ThesisStatus.executed,)))
        )
    }
    traded_fwd = [
        c.fwd_10d_pct for c in cands if c.fwd_10d_pct is not None and c.ticker in traded_tickers
    ]
    skipped_fwd = [
        c.fwd_10d_pct for c in cands if c.fwd_10d_pct is not None and c.ticker not in traded_tickers
    ]
    counterfactual = {
        "note": "fwd returns ~10 sessions after screen; the committee must "
        "beat the candidates it rejected",
        "n_traded": len(traded_fwd),
        "n_skipped": len(skipped_fwd),
        "avg_fwd_traded_pct": (round(sum(traded_fwd) / len(traded_fwd), 2) if traded_fwd else None),
        "avg_fwd_skipped_pct": (
            round(sum(skipped_fwd) / len(skipped_fwd), 2) if skipped_fwd else None
        ),
    }

    from midas.pipeline.events import flag_stats

    return {
        "counterfactual": counterfactual,
        "event_flags": flag_stats(db),
        "self_funding": {
            "realized_pnl_30d": round(pnl_30d, 2),
            "ai_spend_30d": round(spend_30d, 4),
            "ratio": (round(pnl_30d / spend_30d, 1) if spend_30d > 0 else None),
            "covers_costs": bool(spend_30d > 0 and pnl_30d > spend_30d),
            "note": "paper P&L vs real token cost - evidence, not income",
        },
        "closed_positions": n,
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
        "total_pnl_usd": round(total, 2),
        "expectancy_usd": round(expectancy, 2),
        "max_drawdown_usd": round(max_dd, 2),
        "max_drawdown_pct_of_start": round(max_dd / STARTING_CASH * 100, 2),
        "autonomy_level": settings.autonomy_level,
        "eligible_cohort": {
            "note": "broker-backed shares outcomes only; sim option fills excluded",
            "n": n_el,
            "total_pnl_usd": round(total_el, 2),
            "expectancy_usd": round(exp_el, 2),
            "profit_factor": profit_factor,
            "p_expectancy_positive": p_pos,
            "max_drawdown_usd": round(max_dd_el, 2),
        },
        "days_at_l2": days_at_l2,
        "l3_bar": L3_BAR,
        "l3_bar_met": {
            "closed_positions": n_el >= L3_BAR["min_closed_positions"],
            "positive_expectancy": n_el > 0 and exp_el > 0,
            "statistical_confidence": (
                p_pos is not None and p_pos >= L3_BAR.get("min_p_expectancy_positive", 0.9)
            ),
            "drawdown_inside_limit": (
                n_el >= L3_BAR["min_closed_positions"]
                and (max_dd_el / STARTING_CASH * 100) <= L3_BAR["max_drawdown_pct"]
            ),
            "days_at_l2": (
                days_at_l2 is not None and days_at_l2 >= L3_BAR.get("min_days_at_l2", 30)
            ),
        },
        "eligible_for_l3": bool(
            n_el >= L3_BAR["min_closed_positions"]
            and exp_el > 0
            and p_pos is not None
            and p_pos >= L3_BAR.get("min_p_expectancy_positive", 0.9)
            and (max_dd_el / STARTING_CASH * 100) <= L3_BAR["max_drawdown_pct"]
            and days_at_l2 is not None
            and days_at_l2 >= L3_BAR.get("min_days_at_l2", 30)
        ),
    }
