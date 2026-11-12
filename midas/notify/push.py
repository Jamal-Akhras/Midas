"""Real-time pushes: the few ledger events an operator wants the moment
they happen, sent at the end of the monitor pass that produced them.

The brief is the digest (09:00, the open, then hourly); this is the fill,
the stop, the gap flag, the PM action, the halt - one short Telegram
message per pass, only when something happened. The bot token is read
from settings by the caller and never logged (see midas.notify.telegram).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from midas.models import AuditEvent, utcnow
from midas.notify import telegram

logger = logging.getLogger(__name__)

PUSH_TYPES = frozenset(
    {
        "order.filled",
        "order.refused",
        "stop.breach_exit",
        "stop.breach_pending",
        "stop.time",
        "stop.standing_filled",
        "stop.tightened",
        "premarket.gap_flagged",
        "position.exit_at_open",
        "portfolio.action",
        "portfolio.action_refused",
        "thesis.approved",
        "thesis.expired",
        "pipeline.halted",
        "ops.alert",
        "autonomy.l2_started",
    }
)
LABELS = {
    "order.filled": "FILL",
    "order.refused": "REFUSED",
    "stop.breach_exit": "STOP",
    "stop.breach_pending": "STOP PENDING",
    "stop.time": "TIME STOP",
    "stop.standing_filled": "STOP (broker)",
    "stop.tightened": "STOP RAISED",
    "premarket.gap_flagged": "GAP",
    "position.exit_at_open": "EXIT AT OPEN",
    "portfolio.action": "PM",
    "portfolio.action_refused": "PM REFUSED",
    "thesis.approved": "APPROVED",
    "thesis.expired": "EXPIRED",
    "pipeline.halted": "HALT",
    "ops.alert": "ALERT",
    "autonomy.l2_started": "L2 CLOCK",
}
MAX_LINES = 12


def pass_events(db: Session, since: datetime) -> list[AuditEvent]:
    """The pushable events recorded since `since` (with a small margin for
    clock granularity), oldest first."""
    cutoff = since - timedelta(seconds=1)
    return list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.created_at >= cutoff, AuditEvent.event_type.in_(PUSH_TYPES))
            .order_by(AuditEvent.created_at)
        )
    )


def format_push(events: list[AuditEvent], now: datetime | None = None) -> str:
    now = now or utcnow()
    lines = [f"Midas {now:%H:%M} UTC"]
    for e in events[:MAX_LINES]:
        lines.append(f"- {LABELS.get(e.event_type, e.event_type)}: {' '.join(e.summary.split())}")
    if len(events) > MAX_LINES:
        lines.append(f"- and {len(events) - MAX_LINES} more in the ledger")
    return "\n".join(lines)


def push_pass_events(db: Session, since: datetime, *, token: str, chat_id: str) -> int:
    """Send this pass's pushable events as one message. Returns how many
    events were sent; 0 when nothing happened or nothing is configured."""
    if not token or not chat_id:
        return 0
    events = pass_events(db, since)
    if not events:
        return 0
    if telegram.send_message(format_push(events), token=token, chat_id=chat_id):
        return len(events)
    logger.warning("push: %d event(s) not delivered", len(events))
    return 0
