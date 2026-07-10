"""Equity history: one row per snapshot of the paper account, so the
workstation can draw a real equity curve instead of inferring one."""
from datetime import timedelta

from sqlalchemy import Float, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from midas.models.base import Base, PKMixin, TimestampMixin, aware, utcnow


class EquityPoint(PKMixin, TimestampMixin, Base):
    __tablename__ = "equity_points"

    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)


def record_equity_point(db: Session, min_interval_minutes: int = 30) -> EquityPoint | None:
    """Append an equity snapshot unless one exists within the interval.
    Called opportunistically (UI state polls, monitor passes) - dedupe here
    keeps the curve clean no matter how chatty the caller is."""
    last = db.scalar(select(EquityPoint).order_by(EquityPoint.created_at.desc()))
    if last is not None and utcnow() - aware(last.created_at) < timedelta(
            minutes=min_interval_minutes):
        return None
    from midas.execution.paper_broker import get_account

    account = get_account(db)
    point = EquityPoint(equity=account["equity"], cash=account["cash"])
    db.add(point)
    db.flush()
    return point
