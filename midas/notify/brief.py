"""The operator brief: what the ledger says happened since the last brief.

Built from the database alone, with no model or broker calls, so a scheduler
can run it while the monitor loop is live. Its first line is a warning when
the monitor has stopped reporting: a silent loop is the one failure the
broker's own app cannot show.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from midas.config import settings
from midas.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    CostEvent,
    Instrument,
    Position,
    PositionStatus,
    SystemState,
    utcnow,
)

LAST_BRIEF_KEY = "last_brief"
DEFAULT_WINDOW = timedelta(hours=24)
# a pass may legitimately run long (committees); the heartbeat is stale only
# well after the pass it promised
HEARTBEAT_GRACE = timedelta(minutes=10)
MAX_BRIEF_CHARS = 3500
ITEMS_PER_SECTION = 8
SUMMARY_CHARS = 160

ALERT_TYPES = frozenset(
    {
        "ops.alert",
        "ops.correction",
        "execution.mismatch",
        "order.rejected",
        "order.refused",
        "pipeline.halted",
        "halt.cleared",
        "run.failed",
        "cost.divergence",
        "verdict.rejected_sanity",
        "verdict.inconsistent",
        "premarket.gap_flagged",
        "config.changed",
    }
)
TRADE_TYPES = frozenset(
    {
        "order.filled",
        "core.executed",
        "position.reduced",
        "stop.standing_filled",
        "stop.breach_exit",
        "stop.time",
        "portfolio.action",
    }
)
DECISION_TYPES = frozenset(
    {
        "run.completed",
        "core.planned",
        "core.skipped",
        "thesis.proposed",
        "thesis.approved",
        "verdict.overridden",
    }
)


@dataclass
class Brief:
    as_of: datetime
    since: datetime
    heartbeat_at: datetime | None
    heartbeat_stale: bool
    cash: float
    equity: float
    day_change_pct: float | None
    positions: list[dict[str, Any]]
    decisions: list[str]
    trades: list[str]
    alerts: list[str]
    pending_approvals: int
    spend_since_usd: float
    spend_today_usd: float
    daily_cap_usd: float


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _state(db: Session, key: str) -> dict[str, Any]:
    row = db.get(SystemState, key)
    return dict(row.value) if row is not None and row.value else {}


def default_since(db: Session, now: datetime) -> datetime:
    """The window starts where the last sent brief ended; 24 hours on day one."""
    at = _parse_iso(_state(db, LAST_BRIEF_KEY).get("at"))
    return at if at is not None else now - DEFAULT_WINDOW


def mark_brief_sent(db: Session, at: datetime) -> None:
    row = db.get(SystemState, LAST_BRIEF_KEY)
    value = {"at": at.isoformat()}
    if row is None:
        db.add(SystemState(key=LAST_BRIEF_KEY, value=value))
    else:
        row.value = value
    db.flush()


def _heartbeat(db: Session, now: datetime) -> tuple[datetime | None, bool]:
    hb = _state(db, "monitor_heartbeat")
    at = _parse_iso(hb.get("at"))
    if at is None:
        return None, True
    next_at = _parse_iso(hb.get("next_pass_at"))
    if next_at is not None:
        return at, now > next_at + HEARTBEAT_GRACE
    return at, now > at + timedelta(hours=1)


def _spend_since(db: Session, since: datetime) -> float:
    total = db.scalar(
        select(func.coalesce(func.sum(CostEvent.cost_usd), 0.0)).where(
            CostEvent.created_at >= since
        )
    )
    return float(total or 0.0)


def build_brief(
    db: Session, *, since: datetime | None = None, now: datetime | None = None
) -> Brief:
    now = now or utcnow()
    since = since or default_since(db, now)

    account = _state(db, "paper_account")
    cash = float(account.get("cash", 0.0))
    equity = float(account.get("equity", 0.0))
    anchors = _state(db, "equity_anchors")
    day_change: float | None = None
    if anchors.get("day") == now.date().isoformat() and anchors.get("day_equity"):
        day_change = (equity / float(anchors["day_equity"]) - 1.0) * 100.0

    heartbeat_at, stale = _heartbeat(db, now)

    positions: list[dict[str, Any]] = []
    open_rows = db.scalars(
        select(Position).where(Position.status == PositionStatus.open).order_by(Position.symbol)
    )
    for p in open_rows:
        mark = p.last_mark_price or p.avg_price
        multiplier = 100 if p.instrument == Instrument.option else 1
        positions.append(
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_price": p.avg_price,
                "mark": mark,
                "upnl": (mark - p.avg_price) * p.qty * multiplier,
                "book": p.book,
            }
        )

    decisions: list[str] = []
    trades: list[str] = []
    alerts: list[str] = []
    events = db.scalars(
        select(AuditEvent).where(AuditEvent.created_at >= since).order_by(AuditEvent.created_at)
    )
    for e in events:
        if e.event_type in ALERT_TYPES:
            alerts.append(e.summary)
        elif e.event_type in TRADE_TYPES:
            trades.append(e.summary)
        elif e.event_type in DECISION_TYPES and not e.summary.startswith("Screen"):
            decisions.append(e.summary)

    pending = db.scalar(
        select(func.count())
        .select_from(ApprovalRequest)
        .where(ApprovalRequest.status == ApprovalStatus.pending)
    )
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return Brief(
        as_of=now,
        since=since,
        heartbeat_at=heartbeat_at,
        heartbeat_stale=stale,
        cash=cash,
        equity=equity,
        day_change_pct=day_change,
        positions=positions,
        decisions=decisions,
        trades=trades,
        alerts=alerts,
        pending_approvals=int(pending or 0),
        spend_since_usd=_spend_since(db, since),
        spend_today_usd=_spend_since(db, day_start),
        daily_cap_usd=float(settings.daily_spend_limit_usd),
    )


def _clip(text: str, limit: int = SUMMARY_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def format_brief(brief: Brief) -> str:
    """Plain text, no markup: Telegram shows the message as written."""
    lines: list[str] = []
    if brief.heartbeat_stale:
        seen = (
            f"last pass {brief.heartbeat_at:%H:%M} UTC"
            if brief.heartbeat_at is not None
            else "no pass recorded"
        )
        lines.append(f"WARNING: monitor loop is not reporting ({seen}). Check the PC.")
    lines.append(
        f"Midas brief {brief.as_of:%a %d %b %H:%M} UTC, covering since {brief.since:%d %b %H:%M}"
    )
    change = f" ({brief.day_change_pct:+.2f}% today)" if brief.day_change_pct is not None else ""
    lines.append(f"Account: equity ${brief.equity:,.2f}{change}, cash ${brief.cash:,.2f}")
    if brief.positions:
        lines.append("Positions:")
        for p in brief.positions:
            lines.append(
                f"  {p['symbol']} {p['qty']:g} @ {p['avg_price']:.2f}, "
                f"mark {p['mark']:.2f}, P&L {p['upnl']:+.2f}"
            )
    else:
        lines.append("Positions: none")

    def section(title: str, items: list[str]) -> None:
        if not items:
            lines.append(f"{title}: none")
            return
        lines.append(f"{title} ({len(items)}):")
        for item in items[:ITEMS_PER_SECTION]:
            lines.append("  " + _clip(item))
        if len(items) > ITEMS_PER_SECTION:
            lines.append(f"  and {len(items) - ITEMS_PER_SECTION} more")

    section("Alerts", brief.alerts)
    section("Trades", brief.trades)
    section("Decisions", brief.decisions)
    if brief.pending_approvals:
        lines.append(f"{brief.pending_approvals} approval(s) waiting: midas approvals")
    lines.append(
        f"AI spend: ${brief.spend_since_usd:.2f} since last brief, "
        f"${brief.spend_today_usd:.2f} today (cap ${brief.daily_cap_usd:.2f})"
    )
    text = "\n".join(lines)
    if len(text) > MAX_BRIEF_CHARS:
        text = text[: MAX_BRIEF_CHARS - 12].rstrip() + "\n(truncated)"
    return text
