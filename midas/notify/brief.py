"""The operator brief: what the ledger says happened since the last brief.

Built from the database alone, with no model or broker calls, so a scheduler
can run it while the monitor loop is live. Its first line is a warning when
the monitor has stopped reporting: a silent loop is the one failure the
broker's own app cannot show.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from midas.config import settings
from midas.models import (
    SWING_BOOK,
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
    below_stop: list[str] = []
    for p in open_rows:
        mark = p.last_mark_price or p.avg_price
        multiplier = 100 if p.instrument == Instrument.option else 1
        if p.book == SWING_BOOK and p.stop_price and mark < p.stop_price:
            # NVDA 2026-11-12: the 09:00 brief showed a mark under the stop
            # and said "Alerts: none"
            below_stop.append(
                f"{p.symbol} is marked below its stop ({mark:.2f} vs {p.stop_price:.2f}) "
                "on the latest print - the stop layer decides at the open"
            )
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
    alerts: list[str] = list(below_stop)
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


def _local(moment: datetime, tz: tzinfo | None) -> str:
    """The operator reads this on a phone: their clock, not the server's."""
    local = moment.astimezone(tz) if tz is not None else moment.astimezone()
    offset = local.utcoffset() or timedelta(0)
    hours = offset.total_seconds() / 3600
    zone = "UTC" if hours == 0 else f"UTC{hours:+g}"
    return f"{local:%a %d %b %H:%M} {zone}"


def _tidy(summary: str) -> str:
    text = " ".join(summary.split())
    return text.replace("no_trade:", "no trade.").replace("(alpaca_paper)", "at Alpaca")


def _collapse(items: list[str]) -> list[str]:
    """Identical lines become one line with a count: the reconcile step
    repeats a persistent mismatch every pass."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return [f"{item} (x{n})" if n > 1 else item for item, n in counts.items()]


ATTENTION_SPEND_FRACTION = 0.8  # of the daily cap: worth a line even in a quiet hour
COMPACT_ITEMS_PER_SECTION = 3
COMPACT_SPEND_FRACTION = 0.5


def needs_attention(brief: Brief) -> bool:
    """Whether an hourly brief has anything a reviewer should look at: a
    silent monitor, alerts, trades, decisions, approvals, or spend near the
    cap. A quiet hour is reported without spending a review on it."""
    near_cap = (
        brief.daily_cap_usd > 0
        and brief.spend_today_usd >= ATTENTION_SPEND_FRACTION * brief.daily_cap_usd
    )
    return bool(
        brief.heartbeat_stale
        or brief.alerts
        or brief.trades
        or brief.decisions
        or brief.pending_approvals
        or near_cap
    )


def plain_from_html(html_text: str) -> str:
    """The plain-text twin of a rendered message, for the fallback send."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


def _stale_line(brief: Brief, tz: tzinfo | None) -> str:
    seen = (
        f"last pass {_local(brief.heartbeat_at, tz)}"
        if brief.heartbeat_at is not None
        else "no pass recorded"
    )
    return f"\U0001f6a8 <b>Monitor loop is not reporting</b> ({html.escape(seen)}). Check the PC."


def render_compact(brief: Brief, review: str | None = None, *, tz: tzinfo | None = None) -> str:
    """The hourly form: one status line, positions on one line, and only
    the sections that have something in them. A quiet hour is a few lines."""
    esc = html.escape
    lines: list[str] = []
    if brief.heartbeat_stale:
        lines += [_stale_line(brief, tz), ""]
    change = f" ({brief.day_change_pct:+.2f}%)" if brief.day_change_pct is not None else ""
    lines.append(
        f"<b>Midas</b> \u00b7 {esc(_local(brief.as_of, tz))} \u00b7 "
        f"<b>${brief.equity:,.2f}</b>{esc(change)} \u00b7 cash ${brief.cash:,.2f}"
    )
    if brief.positions:
        marks = " \u00b7 ".join(
            f"{esc(str(p['symbol']))} {p['upnl']:+.2f}" for p in brief.positions
        )
        lines.append(f"\U0001f4c8 {marks}")
    else:
        lines.append("\U0001f4c8 no positions")
    if review:
        lines += [esc(line.strip()) for line in review.strip().splitlines() if line.strip()]

    def section(icon: str, title: str, items: list[str]) -> None:
        if not items:
            return
        shown = _collapse([_clip(_tidy(s)) for s in items])
        lines.append(f"{icon} <b>{title} ({len(items)})</b>")
        for item in shown[:COMPACT_ITEMS_PER_SECTION]:
            lines.append("\u2022 " + esc(item))
        if len(shown) > COMPACT_ITEMS_PER_SECTION:
            lines.append(f"\u2022 and {len(shown) - COMPACT_ITEMS_PER_SECTION} more")

    section("\u26a0\ufe0f", "Alerts", brief.alerts)
    section("\U0001f501", "Trades", brief.trades)
    section("\U0001f9ed", "Decisions", brief.decisions)
    if brief.pending_approvals:
        lines.append(
            f"\u270b <b>{brief.pending_approvals} approval(s) waiting</b>: midas approvals"
        )
    near_cap = (
        brief.daily_cap_usd > 0
        and brief.spend_today_usd >= COMPACT_SPEND_FRACTION * brief.daily_cap_usd
    )
    if near_cap:
        lines.append(
            f"\U0001f916 AI spend ${brief.spend_today_usd:.2f} today \u00b7 "
            f"cap ${brief.daily_cap_usd:.2f}"
        )
    if not (brief.alerts or brief.trades or brief.decisions or brief.pending_approvals):
        lines.append(f"<i>quiet since {esc(_local(brief.since, tz))}</i>")
    return "\n".join(lines)


def render_telegram(
    brief: Brief,
    review: str | None = None,
    *,
    tz: tzinfo | None = None,
    compact: bool = False,
) -> str:
    """Telegram HTML: bold headers, the review first, bullets, local time.
    Every piece of ledger or model text is escaped; tags never span lines,
    so the sender may split the message on line breaks. `compact` is the
    hourly form (see render_compact)."""
    if compact:
        return render_compact(brief, review, tz=tz)
    esc = html.escape
    lines: list[str] = []
    if brief.heartbeat_stale:
        seen = (
            f"last pass {_local(brief.heartbeat_at, tz)}"
            if brief.heartbeat_at is not None
            else "no pass recorded"
        )
        lines.append(
            f"\U0001f6a8 <b>Monitor loop is not reporting</b> ({esc(seen)}). Check the PC."
        )
        lines.append("")
    lines.append(f"<b>Midas</b> \u00b7 {esc(_local(brief.as_of, tz))}")
    change = f" ({brief.day_change_pct:+.2f}% today)" if brief.day_change_pct is not None else ""
    lines.append(
        f"\U0001f4b0 <b>Equity ${brief.equity:,.2f}</b>{esc(change)} \u00b7 cash ${brief.cash:,.2f}"
    )
    if review:
        lines += ["", "\U0001f9e0 <b>Review</b>"]
        lines += [esc(line.strip()) for line in review.strip().splitlines() if line.strip()]
    lines += ["", "\U0001f4c8 <b>Positions</b>"]
    if brief.positions:
        for p in brief.positions:
            lines.append(
                f"\u2022 {esc(str(p['symbol']))} {p['qty']:g} @ {p['avg_price']:.2f} "
                f"\u2192 {p['mark']:.2f} ({p['upnl']:+.2f})"
            )
    else:
        lines.append("\u2022 none")

    def section(icon: str, title: str, items: list[str]) -> None:
        lines.append("")
        if not items:
            lines.append(f"{icon} <b>{title}</b>: none")
            return
        shown = _collapse([_clip(_tidy(s)) for s in items])
        lines.append(f"{icon} <b>{title} ({len(items)})</b>")
        for item in shown[:ITEMS_PER_SECTION]:
            lines.append("\u2022 " + esc(item))
        if len(shown) > ITEMS_PER_SECTION:
            lines.append(f"\u2022 and {len(shown) - ITEMS_PER_SECTION} more")

    section("\u26a0\ufe0f", "Alerts", brief.alerts)
    section("\U0001f501", "Trades", brief.trades)
    section("\U0001f9ed", "Decisions", brief.decisions)
    if brief.pending_approvals:
        lines += [
            "",
            f"\u270b <b>{brief.pending_approvals} approval(s) waiting</b>: midas approvals",
        ]
    lines += [
        "",
        f"\U0001f916 AI spend ${brief.spend_since_usd:.2f} since last brief \u00b7 "
        f"${brief.spend_today_usd:.2f} today \u00b7 cap ${brief.daily_cap_usd:.2f}",
        f"<i>covering since {esc(_local(brief.since, tz))}</i>",
    ]
    return "\n".join(lines)
