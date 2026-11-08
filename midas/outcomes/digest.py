"""Weekly digest: the L2 review surface. Instead of per-trade approvals,
the operator reads what the committee did, what it holds, what it closed,
and what it spent - with pointers back into the audit trail."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from midas.config import settings
from midas.execution.paper_broker import get_account
from midas.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    CostEvent,
    Instrument,
    Order,
    OrderStatus,
    Position,
    PositionStatus,
    ReviewCheckpoint,
    TradeOutcome,
    TradeThesis,
    aware,
    utcnow,
)
from midas.outcomes.scoring import stats


def build_digest(db: Session, days: int = 7) -> dict:
    since = utcnow() - timedelta(days=days)

    def recent(rows):
        return [r for r in rows if aware(r.created_at) >= since]

    orders = recent(db.scalars(select(Order)).all())
    fills = [o for o in orders if o.status == OrderStatus.filled]
    outcomes = recent(db.scalars(select(TradeOutcome)).all())
    checkpoints = recent(db.scalars(select(ReviewCheckpoint)).all())
    costs = recent(db.scalars(select(CostEvent)).all())
    halts = recent(
        db.scalars(select(AuditEvent).where(AuditEvent.event_type == "pipeline.halted")).all()
    )
    open_pos = db.scalars(select(Position).where(Position.status == PositionStatus.open)).all()
    pending = db.scalars(
        select(ApprovalRequest).where(ApprovalRequest.status == ApprovalStatus.pending)
    ).all()

    def fmt_position(p: Position) -> dict:
        mark = p.last_mark_price or p.avg_price
        mult = 100 if p.instrument == Instrument.option else 1
        thesis = db.get(TradeThesis, p.thesis_id)
        return {
            "symbol": p.symbol,
            "qty": p.qty,
            "avg_price": p.avg_price,
            "mark": mark,
            "unrealized_usd": round((mark - p.avg_price) * p.qty * mult, 2),
            "next_review": str(p.next_review_at),
            "thesis": (thesis.thesis_summary[:140] if thesis else ""),
        }

    return {
        "window_days": days,
        "autonomy_level": settings.autonomy_level,
        "account": get_account(db),
        "entries_filled": [
            {
                "symbol": o.symbol,
                "qty": o.qty,
                "price": o.filled_price,
                "approved_by": o.approved_by,
                "reason": o.reason.value,
            }
            for o in fills
            if o.reason.value == "entry"
        ],
        "exits_filled": [
            {
                "symbol": o.symbol,
                "qty": o.qty,
                "price": o.filled_price,
                "approved_by": o.approved_by,
                "reason": o.reason.value,
            }
            for o in fills
            if o.reason.value != "entry"
        ],
        "closed_outcomes": [
            {
                "pnl_usd": o.pnl_usd,
                "pnl_pct": o.pnl_pct,
                "holding_days": o.holding_days,
                "exit_kind": o.exit_kind,
                "bull_case_right": o.bull_case_right,
            }
            for o in outcomes
        ],
        "reviews": [
            {"decision": c.decision, "trigger": c.trigger.value, "notes": c.notes}
            for c in checkpoints
        ],
        "open_positions": [fmt_position(p) for p in open_pos],
        "pending_approvals": [{"id": str(a.id), "title": a.title} for a in pending],
        "ai_spend_usd": round(sum(c.cost_usd for c in costs), 4),
        "halts": [h.summary for h in halts],
        "track_record": stats(db),
    }
