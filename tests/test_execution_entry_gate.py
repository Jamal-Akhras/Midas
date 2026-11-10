"""2026-11-10 review, step 1: execution.

R1: the executor accepts the risk engine's fractional size. R2: the shares
entry gate reads the live quote once - a thesis whose stop is already
breached is rejected (never bought); a thesis above its zone waits with one
audit event instead of a rejected order per pass. R3: trims halve a
fractional position and never over-sell. R4: a fill is committed before the
executor returns."""

import pytest

from midas.config import settings
from midas.execution import executor, paper_broker
from midas.models import (
    ApprovalRequest,
    AuditEvent,
    Instrument,
    Order,
    OrderStatus,
    Position,
    PositionStatus,
    TradeThesis,
    utcnow,
)
from midas.models.domain import Direction, PipelineRun, ResearchArtifact, RunKind, ThesisStatus
from midas.scheduler import monitor


def _thesis(db, ticker, *, lo, hi, stop, status=ThesisStatus.approved):
    run = PipelineRun(kind=RunKind.committee, ticker=ticker)
    db.add(run)
    db.flush()
    art = ResearchArtifact(
        run_id=run.id, ticker=ticker, agent_slug="investor", artifact_type="verdict", content={}
    )
    db.add(art)
    db.flush()
    th = TradeThesis(
        run_id=run.id,
        verdict_artifact_id=art.id,
        ticker=ticker,
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=lo,
        entry_zone_high=hi,
        stop_price=stop,
        targets=[hi * 1.1],
        status=status,
    )
    db.add(th)
    db.flush()
    return th


def _open_position(db, ticker, *, qty, avg_price):
    th = _thesis(
        db,
        ticker,
        lo=avg_price - 1,
        hi=avg_price + 1,
        stop=avg_price * 0.95,
        status=ThesisStatus.executed,
    )
    p = Position(
        thesis_id=th.id,
        ticker=ticker,
        symbol=ticker,
        instrument=Instrument.shares,
        qty=qty,
        avg_price=avg_price,
        stop_price=th.stop_price,
        last_mark_price=avg_price,
        opened_at=utcnow(),
    )
    db.add(p)
    db.flush()
    return p


def _quote(monkeypatch, price, sector="Tech"):
    """One live price for both the executor's gate and the simulator's fill."""
    import midas.tools.market_data as md

    class _Q:
        data = {"price": price, "sector": sector}

    monkeypatch.setattr(md, "get_quote", lambda t: _Q())
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: price)


@pytest.fixture(autouse=True)
def _small_account(monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 2)
    monkeypatch.setattr(settings, "fractional_shares", True)
    monkeypatch.setattr(settings, "min_swing_notional_usd", 25.0)
    monkeypatch.setattr(settings, "regime_filter_enabled", False)


# ---- R1 ----


def test_executor_accepts_a_fraction_of_a_share(db, monkeypatch):
    paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
    th = _thesis(db, "GOOGL", lo=330.0, hi=345.0, stop=318.0)
    _quote(monkeypatch, 340.0)
    order = executor.execute_thesis(db, th, approved_by="test")
    assert order.status == OrderStatus.filled
    assert 0 < order.qty < 1  # $10 risk / $27 per share = 0.37 shares
    assert th.status == ThesisStatus.executed
    pos = db.query(Position).one()
    assert pos.qty == order.qty
    assert pos.sector == "Tech"


# ---- R2 ----


def test_entry_refused_when_spot_is_through_the_stop(db, monkeypatch):
    paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
    th = _thesis(db, "GAP", lo=330.0, hi=345.0, stop=318.0)
    _quote(monkeypatch, 310.0)  # gapped through the stop before entry
    with pytest.raises(executor.ExecutionRefused, match="invalidated"):
        executor.execute_thesis(db, th, approved_by="test")
    assert th.status == ThesisStatus.rejected
    assert db.query(Order).count() == 0
    kinds = [e.event_type for e in db.query(AuditEvent)]
    assert "thesis.invalidated_before_entry" in kinds
    # the queue runner does not retry a rejected thesis and adds no noise
    before = db.query(AuditEvent).count()
    assert monitor.execute_approved_theses(db) == 0
    assert db.query(AuditEvent).count() == before


def test_waiting_for_the_zone_logs_once_and_creates_no_orders(db, monkeypatch):
    paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
    th = _thesis(db, "EXT", lo=330.0, hi=345.0, stop=318.0)
    _quote(monkeypatch, 360.0)  # extended above the zone
    for _ in range(3):
        assert monitor.execute_approved_theses(db) == 0
    assert th.status == ThesisStatus.approved  # still waiting, not expired
    assert db.query(Order).count() == 0
    assert db.query(AuditEvent).filter_by(event_type="thesis.waiting").count() == 1
    assert db.query(AuditEvent).filter_by(event_type="order.refused").count() == 0
    assert db.query(AuditEvent).filter_by(event_type="order.rejected").count() == 0
    # the price comes back into the zone: it executes on that pass
    _quote(monkeypatch, 340.0)
    assert monitor.execute_approved_theses(db) == 1
    assert th.status == ThesisStatus.executed


def test_quote_failure_refuses_without_rejecting(db, monkeypatch):
    import midas.tools.market_data as md

    paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
    th = _thesis(db, "NOQ", lo=330.0, hi=345.0, stop=318.0)

    def _boom(t):
        raise RuntimeError("feed down")

    monkeypatch.setattr(md, "get_quote", _boom)
    with pytest.raises(executor.ExecutionRefused, match="quote failed"):
        executor.execute_thesis(db, th, approved_by="test")
    assert th.status == ThesisStatus.approved  # retried next pass
    assert db.query(Order).count() == 0


# ---- R3 ----


def test_trim_halves_a_fractional_position(db, monkeypatch):
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 100.0)
    p = _open_position(db, "FRC", qty=0.75, avg_price=100.0)
    order = executor.propose_or_execute_exit(db, p, decision="trim", reasons=["half"])
    assert isinstance(order, Order)
    assert order.status == OrderStatus.filled
    assert order.qty == 0.375
    assert p.qty == 0.375
    assert p.status == PositionStatus.open


def test_trim_is_skipped_when_the_remainder_would_be_dust(db, monkeypatch):
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 100.0)
    p = _open_position(db, "DST", qty=0.4, avg_price=100.0)  # half = $20 < $25 floor
    assert executor.propose_or_execute_exit(db, p, decision="trim", reasons=["half"]) is None
    assert p.qty == 0.4
    assert db.query(Order).count() == 0
    assert db.query(AuditEvent).filter_by(event_type="exit.trim_skipped").count() == 1


def test_whole_share_trim_never_oversells(db, monkeypatch):
    monkeypatch.setattr(settings, "fractional_shares", False)
    monkeypatch.setattr(settings, "autonomy_level", 1)
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 100.0)
    one = _open_position(db, "ONE", qty=1.0, avg_price=100.0)
    # 1 share: the old max(int(0.5), 1) sold the whole position as a "trim"
    assert executor.propose_or_execute_exit(db, one, decision="trim", reasons=["half"]) is None
    three = _open_position(db, "THREE", qty=3.0, avg_price=100.0)
    req = executor.propose_or_execute_exit(db, three, decision="trim", reasons=["half"])
    assert isinstance(req, ApprovalRequest)
    assert req.context["qty"] == 1


# ---- R4 ----


def test_entry_fill_is_committed_before_the_executor_returns(factory, monkeypatch):
    with factory() as db:
        paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
        th = _thesis(db, "CMT", lo=330.0, hi=345.0, stop=318.0)
        db.commit()
        tid = th.id
    _quote(monkeypatch, 340.0)
    with factory() as db:
        th = db.get(TradeThesis, tid)
        order = executor.execute_thesis(db, th, approved_by="test")
        assert order.status == OrderStatus.filled
        db.rollback()  # whatever the caller does next, the fill must survive
    with factory() as db:
        assert db.query(Order).filter_by(status=OrderStatus.filled).count() == 1
        assert db.query(Position).filter_by(status=PositionStatus.open).count() == 1
        assert db.get(TradeThesis, tid).status == ThesisStatus.executed


def test_exit_fill_is_committed_before_the_executor_returns(factory, monkeypatch):
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 100.0)
    with factory() as db:
        p = _open_position(db, "XIT", qty=0.5, avg_price=100.0)
        db.commit()
        pid = p.id
    with factory() as db:
        p = db.get(Position, pid)
        order = executor.propose_or_execute_exit(db, p, decision="exit", reasons=["done"])
        assert isinstance(order, Order)
        assert order.status == OrderStatus.filled
        db.rollback()
    with factory() as db:
        assert db.get(Position, pid).status == PositionStatus.closed
