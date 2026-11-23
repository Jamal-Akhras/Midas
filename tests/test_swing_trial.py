"""2026-11-23: the swing book is a trial, and its stop rule is written first.

Five swing trades, five losses, and a 22-year test of every entry rule the
live record suggested found no edge in the setup. The book keeps a small
budget, on one condition set before the next trade: after
SWING_TRIAL_TRADES closed trades opened on or after SWING_TRIAL_START, an
average result at or below zero switches new swing entries off - the
research spend with them. Open positions keep their stops and reviews.
The verdict is read from the first N trades only, so a later winner cannot
reopen a trial that failed and a later loser cannot close one that passed."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.execution import paper_broker
from midas.models import CORE_BOOK, AuditEvent, PositionStatus, TradeOutcome
from midas.risk import engine, trial
from midas.scheduler import monitor
from tests.test_stop_guard import _position


@pytest.fixture(autouse=True)
def _rule(monkeypatch):
    monkeypatch.setattr(settings, "swing_trial_trades", 3)
    monkeypatch.setattr(settings, "swing_trial_start", "2026-11-20")


def _closed(db, ticker: str, pnl_pct: float, *, opened: str = "2026-11-21", day: int = 1):
    p = _position(db, ticker)
    p.opened_at = datetime.fromisoformat(opened + "T15:00:00+00:00")
    p.closed_at = datetime(2026, 12, day, 15, 0, tzinfo=timezone.utc)
    p.status = PositionStatus.closed
    p.qty = 0.0
    db.add(
        TradeOutcome(
            position_id=p.id,
            thesis_id=p.thesis_id,
            pnl_usd=pnl_pct,
            pnl_pct=pnl_pct,
            holding_days=1,
            exit_kind="stop",
        )
    )
    db.flush()
    return p


def _ended_events(db) -> list[AuditEvent]:
    return list(db.scalars(select(AuditEvent).where(AuditEvent.event_type == "swing.trial_ended")))


# ---------- the verdict ----------


def test_no_trial_is_configured_by_default(db, monkeypatch):
    monkeypatch.setattr(settings, "swing_trial_trades", 0)
    _closed(db, "AAA", -5.0)
    status = trial.status(db)
    assert status.active is False and status.ended is False and status.line() == ""


def test_a_running_trial_counts_its_closed_trades(db):
    _closed(db, "AAA", -2.0, day=1)
    _closed(db, "BBB", -1.0, day=2)
    status = trial.status(db)
    assert (status.active, status.closed, status.needed, status.ended) == (True, 2, 3, False)
    assert status.line() == (
        "swing trial: 2 of 3 closed trades since 2026-11-20, average -1.50% - running"
    )


def test_an_average_at_or_below_zero_ends_the_trial(db):
    for i, pnl in enumerate((-2.0, 3.0, -1.0), 1):
        _closed(db, f"T{i}", pnl, day=i)
    status = trial.status(db)
    assert status.ended is True and status.average_pct == 0.0
    assert status.line() == (
        "swing trial: 3 of 3 closed trades since 2026-11-20, average +0.00% - ended: no new "
        "swing entries"
    )


def test_a_positive_average_passes_the_trial(db):
    for i, pnl in enumerate((-2.0, 3.0, 0.5), 1):
        _closed(db, f"T{i}", pnl, day=i)
    status = trial.status(db)
    assert status.ended is False
    assert status.line().endswith("average +0.50% - passed")


def test_only_the_first_trades_of_the_trial_are_read(db):
    for i, pnl in enumerate((-2.0, -1.0, -3.0), 1):
        _closed(db, f"T{i}", pnl, day=i)
    _closed(db, "LATE", 50.0, day=9)  # a later winner does not reopen it
    status = trial.status(db)
    assert status.ended is True and status.average_pct == -2.0 and status.closed == 3


def test_earlier_trades_and_the_core_book_are_not_in_the_trial(db):
    _closed(db, "OLD", -9.0, opened="2026-11-13")  # opened before the trial began
    core = _closed(db, "SPY", -4.0)
    core.book = CORE_BOOK
    db.flush()
    _closed(db, "NEW", 1.0)
    status = trial.status(db)
    assert status.closed == 1 and status.average_pct == 1.0


# ---------- the switch ----------


def test_the_end_is_recorded_once(db):
    for i in range(1, 4):
        _closed(db, f"T{i}", -1.0, day=i)
    assert trial.enforce(db).ended is True
    assert trial.enforce(db).ended is True
    (event,) = _ended_events(db)
    assert event.summary == (
        "swing trial: 3 of 3 closed trades since 2026-11-20, average -1.00% - ended: no new "
        "swing entries"
    )
    assert event.payload == {"closed": 3, "needed": 3, "average_pct": -1.0, "start": "2026-11-20"}


def test_an_ended_trial_fails_the_risk_check(db):
    rc = engine.check_shares_trade(db, account_equity=100_000, entry_price=100.0, stop_price=95.0)
    assert "swing_trial" not in rc.checks  # nothing closed yet is not a check worth a line
    for i in range(1, 4):
        _closed(db, f"T{i}", -1.0, day=i)
    rc = engine.check_shares_trade(db, account_equity=100_000, entry_price=100.0, stop_price=95.0)
    assert not rc.ok and rc.checks["swing_trial"]["ok"] is False
    assert rc.checks["swing_trial"]["value"] == "3 of 3 closed, average -1.00%"


def test_an_ended_trial_stops_the_daily_cycle_and_the_entries(factory, demo_router, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(monitor, "market_session_open", lambda: True)
    monkeypatch.setattr(
        monitor, "market_regime", lambda db: {"risk_on": True, "detail": "test", "date": ""}
    )
    monkeypatch.setattr(monitor, "execute_approved_theses", lambda db: calls.append("entries") or 0)
    monkeypatch.setattr(monitor, "maybe_portfolio_review", lambda *a, **k: None)
    monkeypatch.setattr(settings, "event_scan_weekdays", "")
    monkeypatch.setattr(settings, "core_enabled", False)
    with factory() as db:
        paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
        for i in range(1, 4):
            _closed(db, f"T{i}", -1.0, day=i)
        db.commit()
    summary = monitor.run_monitor_once(factory, demo_router)
    assert calls == []
    assert summary["swing_trial"].endswith("ended: no new swing entries")
    with factory() as db:
        assert len(_ended_events(db)) == 1


def test_an_ended_trial_spends_nothing_on_research(factory, demo_router, monkeypatch):
    import midas.pipeline.screen as screen

    monkeypatch.setattr(settings, "auto_cycle_weekdays", "mon,tue,wed,thu,fri,sat,sun")
    monkeypatch.setattr(monitor, "_minutes_since_open", lambda: 75.0)
    calls: list[str] = []
    monkeypatch.setattr(
        screen, "run_screen", lambda f, r: calls.append("screen") or {"candidates": []}
    )
    with factory() as db:
        for i in range(1, 4):
            _closed(db, f"T{i}", -1.0, day=i)
        db.commit()
    assert monitor.maybe_auto_cycle(factory, demo_router) is None
    assert calls == []
    monkeypatch.setattr(settings, "swing_trial_trades", 0)  # no trial: the cycle runs
    assert monitor.maybe_auto_cycle(factory, demo_router) is not None
    assert calls == ["screen"]
