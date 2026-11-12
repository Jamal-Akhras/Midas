"""2026-11-12: the opening guard on stop-breach exits.

NVDA's 159.43 stop was hit on the opening flush at 155.60, four minutes
into the session, and the stock traded 162.83 twenty minutes later. The
monitor's stop layer now: exits at once only through a DISASTER FLOOR
(stop minus half the original risk unit, STOP_DISASTER_MULTIPLE 1.5);
skips ordinary breaches inside the first STOP_OPENING_GUARD_MINUTES; and
after that needs the breach on STOP_CONFIRMATION_PASSES consecutive
passes. Code still owns the exit - the reevaluator never gets a vote."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.models import (
    SWING_BOOK,
    ArtifactType,
    AuditEvent,
    CheckpointTrigger,
    Instrument,
    Position,
    ResearchArtifact,
    SystemState,
    ThesisStatus,
    TradeThesis,
    utcnow,
)
from midas.models.domain import Direction, PipelineRun, RunKind
from midas.scheduler import monitor


@pytest.fixture(autouse=True)
def _guarded(monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 2)
    monkeypatch.setattr(settings, "stop_opening_guard_minutes", 30)
    monkeypatch.setattr(settings, "stop_confirmation_passes", 2)
    monkeypatch.setattr(settings, "stop_disaster_multiple", 1.5)
    monkeypatch.setattr(settings, "earnings_lookahead_days", 0)


def _position(db, ticker="NVDA", *, avg=100.0, stop=95.0):
    run = PipelineRun(kind=RunKind.committee, ticker=ticker)
    db.add(run)
    db.flush()
    art = ResearchArtifact(
        run_id=run.id,
        ticker=ticker,
        agent_slug="investor",
        artifact_type=ArtifactType.verdict,
        content={},
    )
    db.add(art)
    db.flush()
    th = TradeThesis(
        run_id=run.id,
        verdict_artifact_id=art.id,
        ticker=ticker,
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=avg,
        entry_zone_high=avg,
        stop_price=stop,
        targets=[avg * 1.1],
        status=ThesisStatus.executed,
        book=SWING_BOOK,
    )
    db.add(th)
    db.flush()
    p = Position(
        thesis_id=th.id,
        ticker=ticker,
        symbol=ticker,
        instrument=Instrument.shares,
        qty=1.0,
        avg_price=avg,
        stop_price=stop,
        last_mark_price=avg,
        opened_at=utcnow() - timedelta(days=1),
        next_review_at=utcnow() + timedelta(days=1),
    )
    db.add(p)
    db.flush()
    return p


def _at(monkeypatch, *, minutes_since_open, price):
    monkeypatch.setattr(monitor, "_minutes_since_open", lambda: float(minutes_since_open))
    monkeypatch.setattr(monitor, "_underlying_price", lambda db, p: float(price))


def _breaches(db) -> dict:
    row = db.get(SystemState, "stop_breaches")
    return dict(row.value) if row else {}


def _events(db, kind) -> list[str]:
    return [e.summary for e in db.scalars(select(AuditEvent).where(AuditEvent.event_type == kind))]


# ---------- the guard ----------


def test_an_opening_flush_is_not_an_exit(db, monkeypatch):
    _position(db)  # risk unit 5, floor 92.5
    _at(monkeypatch, minutes_since_open=4, price=93.0)
    assert monitor.due_positions(db) == []
    (note,) = _events(db, "stop.breach_pending")
    assert "NVDA" in note and "opening guard" in note


def test_the_disaster_floor_exits_at_once(db, monkeypatch):
    _position(db)
    _at(monkeypatch, minutes_since_open=4, price=92.0)
    due = monitor.due_positions(db)
    assert [d[1] for d in due] == [CheckpointTrigger.invalidation]
    assert "disaster floor 92.5" in due[0][2]


def test_after_the_open_a_breach_needs_two_consecutive_passes(db, monkeypatch):
    p = _position(db)
    _at(monkeypatch, minutes_since_open=60, price=94.0)
    assert monitor.due_positions(db) == []
    assert _breaches(db)[str(p.id)]["count"] == 1
    due = monitor.due_positions(db)
    assert [d[1] for d in due] == [CheckpointTrigger.invalidation]
    assert "confirmed on 2 consecutive passes" in due[0][2]
    assert str(p.id) not in _breaches(db)  # the exit clears the count


def test_reclaiming_the_stop_resets_the_count(db, monkeypatch):
    p = _position(db)
    _at(monkeypatch, minutes_since_open=60, price=94.0)
    monitor.due_positions(db)
    _at(monkeypatch, minutes_since_open=75, price=96.0)
    assert monitor.due_positions(db) == []
    assert str(p.id) not in _breaches(db)
    assert _events(db, "stop.breach_cleared") == ["NVDA: reclaimed its stop 95.0 (96.0)"]
    _at(monkeypatch, minutes_since_open=90, price=94.0)
    assert monitor.due_positions(db) == []  # the count starts again at 1
    assert _breaches(db)[str(p.id)]["count"] == 1


def test_nvda_replay_holds_through_the_flush(db, monkeypatch):
    _position(db, avg=172.116, stop=159.43)  # risk unit 12.686, floor 153.09
    _at(monkeypatch, minutes_since_open=4, price=155.6)
    assert monitor.due_positions(db) == []
    _at(monkeypatch, minutes_since_open=34, price=162.83)
    assert monitor.due_positions(db) == []
    assert _breaches(db) == {}


def test_pending_is_noted_once_per_day(db, monkeypatch):
    _position(db)
    _at(monkeypatch, minutes_since_open=4, price=93.0)
    monitor.due_positions(db)
    _at(monkeypatch, minutes_since_open=19, price=93.5)
    monitor.due_positions(db)
    _at(monkeypatch, minutes_since_open=49, price=93.5)  # past the guard: counting
    monitor.due_positions(db)
    assert len(_events(db, "stop.breach_pending")) == 1


def test_guard_off_is_the_old_behaviour(db, monkeypatch):
    monkeypatch.setattr(settings, "stop_opening_guard_minutes", 0)
    monkeypatch.setattr(settings, "stop_confirmation_passes", 1)
    _position(db)
    _at(monkeypatch, minutes_since_open=4, price=94.0)
    due = monitor.due_positions(db)
    assert [d[1] for d in due] == [CheckpointTrigger.invalidation]
    assert "breached stop 95.0" in due[0][2]


def test_unknown_session_clock_does_not_hold_a_breach_forever(db, monkeypatch):
    _position(db)
    monkeypatch.setattr(monitor, "_minutes_since_open", lambda: None)
    monkeypatch.setattr(monitor, "_underlying_price", lambda db, p: 94.0)
    assert monitor.due_positions(db) == []  # pass 1 of 2
    assert [d[1] for d in monitor.due_positions(db)] == [CheckpointTrigger.invalidation]


def test_guard_settings_are_part_of_the_strategy_fingerprint(monkeypatch):
    before = monitor._strategy_fingerprint()
    monkeypatch.setattr(settings, "stop_opening_guard_minutes", 45)
    assert monitor._strategy_fingerprint() != before
    second = monitor._strategy_fingerprint()
    monkeypatch.setattr(settings, "stop_disaster_multiple", 2.0)
    assert monitor._strategy_fingerprint() != second


def test_minutes_since_open_reads_the_new_york_clock(monkeypatch):
    real = monitor._real_market_fns["minutes"]
    # 14:34 UTC in November = 09:34 New York
    monkeypatch.setattr(
        monitor, "utcnow", lambda: datetime(2026, 11, 12, 14, 34, tzinfo=timezone.utc)
    )
    assert real() == 4.0
    monkeypatch.setattr(
        monitor, "utcnow", lambda: datetime(2026, 11, 12, 15, 4, tzinfo=timezone.utc)
    )
    assert real() == 34.0
