"""Stop protection: breakeven ratchet, trailing, time stops, standing stops."""
from datetime import timedelta

from midas.config import settings
from midas.execution import paper_broker
from midas.models import (
    ApprovalRequest, Instrument, Position, TradeThesis, utcnow,
)
from midas.models.domain import Direction, PipelineRun, ResearchArtifact, RunKind, ThesisStatus
from midas.scheduler import monitor
from midas.tools.base import ToolResult


def _position(db, ticker="ACME", avg=100.0, thesis_stop=95.0, mark=None,
              pos_stop=0.0, opened_days_ago=1):
    run = PipelineRun(kind=RunKind.committee, ticker=ticker)
    db.add(run)
    db.flush()
    art = ResearchArtifact(run_id=run.id, ticker=ticker, agent_slug="investor",
                           artifact_type="verdict", content={})
    db.add(art)
    db.flush()
    thesis = TradeThesis(
        run_id=run.id, verdict_artifact_id=art.id, ticker=ticker,
        direction=Direction.bullish, instrument=Instrument.shares,
        entry_zone_low=avg - 1, entry_zone_high=avg + 1, stop_price=thesis_stop,
        status=ThesisStatus.executed,
    )
    db.add(thesis)
    db.flush()
    p = Position(thesis_id=thesis.id, ticker=ticker, symbol=ticker,
                 instrument=Instrument.shares, qty=10, avg_price=avg,
                 stop_price=pos_stop, last_mark_price=mark,
                 opened_at=utcnow() - timedelta(days=opened_days_ago))
    db.add(p)
    db.flush()
    return p


def _fake_bars(lows):
    def fake(ticker, days=10):
        return ToolResult(data=[{"low": lo, "high": lo + 2, "open": lo + 1,
                                 "close": lo + 1, "date": "2026-07-01",
                                 "volume": 1e6} for lo in lows], source="test")
    return fake


def test_breakeven_then_trail(db, monkeypatch):
    import midas.tools.market_data as md

    monkeypatch.setattr(md, "get_daily_bars", _fake_bars([101.5, 102.0, 103.0]))
    p = _position(db, avg=100.0, thesis_stop=95.0, mark=106.0)  # up 1.2R
    changes = monitor.ratchet_stops(db)
    assert changes and changes[0]["to"] == 101.5  # breakeven, then 10d low
    assert p.stop_price == 101.5


def test_no_ratchet_below_one_r(db, monkeypatch):
    p = _position(db, avg=100.0, thesis_stop=95.0, mark=103.0)  # only 0.6R
    assert monitor.ratchet_stops(db) == []
    assert p.stop_price == 0.0  # untouched; thesis stop still effective


def test_ratchet_is_monotonic_and_below_market(db, monkeypatch):
    import midas.tools.market_data as md

    # trail ABOVE the mark (gap down): breakeven still applies, trail ignored
    monkeypatch.setattr(md, "get_daily_bars", _fake_bars([107.0]))
    p = _position(db, avg=100.0, thesis_stop=95.0, mark=106.0)
    monitor.ratchet_stops(db)
    assert p.stop_price == 100.0  # breakeven, not 107


def test_time_stop_proposes_exit_once(db, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 0)  # proposals, not orders
    p = _position(db, opened_days_ago=30, mark=101.0)
    out = monitor.apply_time_stops(db)
    assert out == ["ACME"]
    reqs = db.query(ApprovalRequest).all()
    assert len(reqs) == 1
    assert monitor.apply_time_stops(db) == []  # pending approval blocks repeat
    assert len(db.query(ApprovalRequest).all()) == 1


def test_time_stop_respects_holding_limit(db):
    _position(db, opened_days_ago=5, mark=101.0)
    assert monitor.apply_time_stops(db) == []


def test_standing_stops_disabled_without_keys(db, monkeypatch):
    monkeypatch.setattr(settings, "alpaca_api_key", "")
    assert paper_broker.sync_standing_stops(db) == {"enabled": False}


def test_effective_stop_falls_back_to_thesis(db):
    p = _position(db, thesis_stop=95.0, pos_stop=0.0)
    assert paper_broker.effective_stop(db, p) == 95.0
    p.stop_price = 98.5
    assert paper_broker.effective_stop(db, p) == 98.5


def test_stop_breach_exit_is_deterministic(factory, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 0)
    with factory() as db:
        p = _position(db, opened_days_ago=2, mark=94.0)  # below the 95 stop
        db.commit()
        pid = p.id
    assert monitor.execute_stop_exit(factory, pid, "underlying 94.0 breached stop 95.0") == "exit"
    with factory() as db:
        reqs = db.query(ApprovalRequest).all()
        assert len(reqs) == 1  # L0: proposed to operator, not silently dropped
    # second pass: still pending -> no duplicate proposal
    assert monitor.execute_stop_exit(factory, pid, "again") == "awaiting operator"


def test_fail_closed_when_broker_unavailable(db, monkeypatch):
    from midas.models import Instrument as I, Order, OrderReason, OrderSide, OrderStatus

    monkeypatch.setattr(settings, "alpaca_mirror_orders", True)
    monkeypatch.setattr(settings, "require_broker_fills", True)
    monkeypatch.setattr(settings, "alpaca_api_key", "x")
    monkeypatch.setattr(settings, "alpaca_api_secret", "y")
    monkeypatch.setattr(paper_broker, "_mirror_to_alpaca", lambda o: None)
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 100.0)
    cash_before = paper_broker.get_account(db)["cash"]
    th = _position(db, ticker="ACME")  # gives us a valid thesis id
    order = Order(thesis_id=th.thesis_id, side=OrderSide.buy, instrument=I.shares,
                  symbol="ACME", qty=10, reason=OrderReason.entry,
                  status=OrderStatus.approved)
    db.add(order)
    db.flush()
    paper_broker.submit(db, order)
    assert order.status == OrderStatus.rejected
    assert "fail-closed" in order.error
    assert paper_broker.get_account(db)["cash"] == cash_before  # nothing moved


def test_option_fills_carry_friction(db, monkeypatch):
    from midas.models import Instrument as I, Order, OrderReason, OrderSide, OrderStatus

    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 4.0)
    cash_before = paper_broker.get_account(db)["cash"]
    th = _position(db, ticker="ACME")
    order = Order(thesis_id=th.thesis_id, side=OrderSide.buy, instrument=I.option,
                  symbol="ACME_2026-08-21_call_100.0", qty=2,
                  reason=OrderReason.entry, status=OrderStatus.approved)
    db.add(order)
    db.flush()
    paper_broker.submit(db, order)
    assert order.filled_price == 4.04  # 1% through the mid
    spent = cash_before - paper_broker.get_account(db)["cash"]
    import pytest as _pt

    assert spent == _pt.approx(4.04 * 2 * 100 + 0.65 * 2)  # notional + fees


def test_marked_drawdown_halts_before_any_close(db, monkeypatch):
    from midas.risk import engine as eng

    eng.update_equity_anchors(db, 100_000.0)
    # open drawdown of 4% with ZERO closed trades must trip the daily halt
    reason = eng.loss_limits_breached(db, account_equity=96_000.0)
    assert reason and "daily drawdown" in reason
    assert eng.loss_limits_breached(db, account_equity=99_000.0) is None


def test_unknown_sector_fails_closed(db, monkeypatch):
    from midas.risk import engine as eng

    monkeypatch.setattr(settings, "max_positions_per_sector", 1)
    _position(db, ticker="MYST")  # sector defaults empty -> UNKNOWN bucket
    p = db.query(Position).first()
    p.sector = ""
    db.flush()
    rc = eng.check_shares_trade(db, account_equity=100_000,
                                entry_price=100, stop_price=95, sector="")
    assert not rc.ok
    assert not rc.checks["sector_concentration"]["ok"]


def test_l2_ledger_and_eligibility_flag(db, monkeypatch):
    from midas.models import SystemState
    from midas.outcomes.scoring import stats

    monkeypatch.setattr(settings, "autonomy_level", 2)
    monitor._record_l2_ledger(db)
    row = db.get(SystemState, "l2_since")
    assert row is not None and row.value.get("at")
    s = stats(db)
    assert s["days_at_l2"] == 0
    assert s["eligible_for_l3"] is False  # zero closed trades can never pass
    assert s["l3_bar_met"]["drawdown_inside_limit"] is False  # no free pass at n=0
