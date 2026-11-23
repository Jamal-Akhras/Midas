"""The swing book as a trial with its stop rule written in advance.

By 2026-11-23 the swing book had made five trades and lost on all five,
and a 22-year test of every entry rule that record suggested (the reclaim
gate, the index condition at entry, buying the dip, momentum ranking, the
gap-and-hold) found no edge in the setup (docs/results.md). The book keeps
a small budget on one condition, fixed before the next trade: after
SWING_TRIAL_TRADES closed trades opened on or after SWING_TRIAL_START, an
average result at or below zero ends it. Ended means no new swing entries
and no research spend on them; open positions keep their stops and
reviews.

The verdict is read from the first N trades of the trial, in the order
they closed, and from nothing after them: a later winner cannot reopen a
trial that failed and a later loser cannot close one that passed. A new
trial is a new SWING_TRIAL_START, which is an operator's decision and
restarts the evidence clock like any other change of strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from midas.audit import record_event
from midas.config import settings
from midas.models import SWING_BOOK, Position, SystemState, TradeOutcome, aware, utcnow

ENDED_KEY = "swing_trial_ended"


@dataclass(frozen=True)
class TrialStatus:
    active: bool = False  # a trial is configured
    start: str = ""
    needed: int = 0
    closed: int = 0  # trades read so far, never more than `needed`
    average_pct: float | None = None
    ended: bool = False  # the rule has switched new swing entries off

    def value(self) -> str:
        """The count and the average, for a risk-check line."""
        average = "" if self.average_pct is None else f", average {self.average_pct:+.2f}%"
        return f"{self.closed} of {self.needed} closed{average}"

    def line(self) -> str:
        """One line for the pass summary and the audit trail; empty when no
        trial is configured."""
        if not self.active:
            return ""
        since = f" since {self.start}" if self.start else ""
        average = "" if self.average_pct is None else f", average {self.average_pct:+.2f}%"
        if self.ended:
            verdict = "ended: no new swing entries"
        elif self.closed >= self.needed:
            verdict = "passed"
        else:
            verdict = "running"
        return (
            f"swing trial: {self.closed} of {self.needed} closed trades{since}{average} - {verdict}"
        )


def _start() -> tuple[str, datetime | None]:
    raw = settings.swing_trial_start.strip()
    if not raw:
        return "", None
    day = date.fromisoformat(raw[:10])  # a malformed date is an error, not "every trade"
    return day.isoformat(), datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def status(db: Session) -> TrialStatus:
    needed = int(settings.swing_trial_trades)
    if needed <= 0:
        return TrialStatus()
    label, since = _start()
    rows = db.execute(
        select(TradeOutcome, Position)
        .join(Position, Position.id == TradeOutcome.position_id)
        .where(Position.book == SWING_BOOK)
    ).all()
    trades = [
        (aware(position.closed_at or outcome.created_at), float(outcome.pnl_pct))
        for outcome, position in rows
        if since is None or aware(position.opened_at) >= since
    ]
    trades.sort(key=lambda t: t[0])
    read = [pnl for _, pnl in trades[:needed]]
    average = round(sum(read) / len(read), 4) if read else None
    ended = len(read) >= needed and average is not None and average <= 0
    return TrialStatus(
        active=True,
        start=label,
        needed=needed,
        closed=len(read),
        average_pct=average,
        ended=ended,
    )


def enforce(db: Session) -> TrialStatus:
    """The status, with the end written to the audit trail the first time a
    pass sees it. The caller owns the transaction."""
    current = status(db)
    if not current.ended:
        return current
    trial_id = {"start": current.start, "needed": current.needed}
    row = db.get(SystemState, ENDED_KEY)
    if row is not None and {k: row.value.get(k) for k in trial_id} == trial_id:
        return current
    record_event(
        db,
        event_type="swing.trial_ended",
        summary=current.line(),
        payload={
            "closed": current.closed,
            "needed": current.needed,
            "average_pct": current.average_pct,
            "start": current.start,
        },
    )
    value = {**trial_id, "at": utcnow().isoformat(), "average_pct": current.average_pct}
    if row is None:
        db.add(SystemState(key=ENDED_KEY, value=value))
    else:
        row.value = value
    db.flush()
    return current
