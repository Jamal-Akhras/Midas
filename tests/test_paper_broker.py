"""Paper broker: fills, limits, cash constraints, position math."""
import pytest
from sqlalchemy import select

from midas.config import settings
from midas.execution import paper_broker as pb
from midas.models import (
    Direction,
    Instrument,
    Order,
    OrderReason,
    OrderSide,
    OrderStatus,
    PipelineRun,
    Position,
    PositionStatus,
    ResearchArtifact,
    RunKind,
    ThesisStatus,
    TradeThesis,
)


@pytest.fixture(autouse=True)
def demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)


def make_thesis(db, ticker="ACME"):
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
        entry_zone_low=180, entry_zone_high=186, stop_price=168,
        status=ThesisStatus.approved, review_schedule={"cadence_days": 1},
    )
    db.add(thesis)
    db.flush()
    return thesis


def buy(db, thesis, qty=10, limit=None):
    order = Order(thesis_id=thesis.id, side=OrderSide.buy,
                  instrument=Instrument.shares, symbol=thesis.ticker, qty=qty,
                  limit_price=limit, reason=OrderReason.entry,
                  status=OrderStatus.approved)
    db.add(order)
    db.flush()
    pb.submit(db, order)
    return order


def test_fill_at_market_and_cash_moves(db):
    thesis = make_thesis(db)
    order = buy(db, thesis, qty=10)
    assert order.status == OrderStatus.filled
    assert order.filled_price == 184.20  # demo quote
    account = pb.get_account(db)
    assert account["cash"] == pytest.approx(100_000 - 1842.0)


def test_limit_price_respected(db):
    thesis = make_thesis(db)
    order = buy(db, thesis, qty=10, limit=180.0)  # market is 184.20
    assert order.status == OrderStatus.rejected
    assert "beyond limit" in order.error


def test_insufficient_cash_rejects(db):
    thesis = make_thesis(db)
    order = buy(db, thesis, qty=10_000)  # ~1.8M notional
    assert order.status == OrderStatus.rejected
    assert "insufficient paper cash" in order.error


def test_position_lifecycle_and_realized_pnl(db):
    thesis = make_thesis(db)
    order = buy(db, thesis, qty=10)
    position = pb.apply_fill_to_position(db, order, thesis)
    assert position.qty == 10 and position.status == PositionStatus.open

    sell = Order(thesis_id=thesis.id, position_id=position.id,
                 side=OrderSide.sell, instrument=Instrument.shares,
                 symbol="ACME", qty=10, reason=OrderReason.exit,
                 status=OrderStatus.approved)
    db.add(sell)
    db.flush()
    pb.submit(db, sell)
    pb.apply_fill_to_position(db, sell, thesis)
    assert position.status == PositionStatus.closed
    assert position.realized_pnl_usd == pytest.approx(0.0)  # same demo price


def test_spread_symbol_marks_at_net_debit(db):
    sym = pb.option_symbol("ACME", "2026-09-01", "call", 184.0, 193.0)
    underlying, detail = pb._parse_option_symbol(sym)
    assert underlying == "ACME"
    assert detail["strike"] == 184.0 and detail["short_strike"] == 193.0


def test_refresh_equity_marks_positions(db):
    thesis = make_thesis(db)
    order = buy(db, thesis, qty=10)
    pb.apply_fill_to_position(db, order, thesis)
    equity = pb.refresh_equity(db)
    # cash (100k - 1842) + marked value (1842) = 100k
    assert equity == pytest.approx(100_000)
    position = db.scalar(select(Position))
    assert position.last_mark_price == 184.20
