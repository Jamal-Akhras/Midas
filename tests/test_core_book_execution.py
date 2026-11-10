"""Core rebalance execution: the month is stamped only after the plan has
actually been executed (or proposed at L0), an order that raises is retried
on later passes (up to a small cap), broker rejections are this month's
answer, top-ups add to the existing position, and a thesis whose first buy
never filled is not left 'approved' for anything else to retry."""

import uuid

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.execution import paper_broker
from midas.models import (
    CORE_BOOK,
    AuditEvent,
    Instrument,
    Order,
    OrderSide,
    OrderStatus,
    PipelineRun,
    Position,
    PositionStatus,
    RunStatus,
    SystemState,
    ThesisStatus,
    TradeThesis,
    utcnow,
)
from midas.strategy import core
from midas.tools.base import ToolError
from midas.tools.registry import TOOLS


@pytest.fixture(autouse=True)
def core_settings(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 1)
    monkeypatch.setattr(settings, "core_enabled", True)
    monkeypatch.setattr(settings, "core_universe", "ACME,BOLT,DUNE")
    monkeypatch.setattr(settings, "core_allocation_pct", 60.0)
    monkeypatch.setattr(settings, "core_trend_window", 20)
    monkeypatch.setattr(settings, "fractional_shares", True)


def _events(db, event_type: str) -> list[AuditEvent]:
    return list(db.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type)))


def _theses(db, ticker: str, status: ThesisStatus) -> list[TradeThesis]:
    return list(
        db.scalars(
            select(TradeThesis).where(
                TradeThesis.ticker == ticker,
                TradeThesis.book == CORE_BOOK,
                TradeThesis.status == status,
            )
        )
    )


# ---------- K3: top-ups and duplicate rows ----------


def test_top_up_buy_adds_to_the_existing_position_instead_of_a_second_row(db):
    core.run_rebalance(db, force=True)
    db.commit()
    before = {p.ticker: (p.id, p.qty) for p in core.core_positions(db)}
    assert len(before) == 3
    account = paper_broker.get_account(db)
    paper_broker.set_account(db, cash=account["cash"] + 50_000, equity=account["equity"] + 50_000)

    plan = core.build_plan(db)
    assert plan.orders and all(o.side == "buy" for o in plan.orders)
    result = core.run_rebalance(db, force=True)
    db.commit()
    assert result["status"] == "executed"

    positions = core.core_positions(db)
    assert len(positions) == 3  # still one row per ticker
    for p in positions:
        row_id, qty_before = before[p.ticker]
        assert p.id == row_id and p.qty > qty_before
    top_ups = db.scalars(
        select(Order).where(Order.side == OrderSide.buy).order_by(Order.created_at)
    ).all()[3:]
    assert len(top_ups) == 3
    assert all(o.position_id == before[o.symbol][0] for o in top_ups)
    assert all(o.status == OrderStatus.filled for o in top_ups)


def test_duplicate_rows_are_summed_and_a_trend_exit_sells_all_of_them(db, monkeypatch):
    core.run_rebalance(db, force=True)
    db.commit()
    first = db.scalar(select(Position).where(Position.ticker == "BOLT"))
    assert first is not None
    second = Position(
        thesis_id=first.thesis_id,
        ticker="BOLT",
        symbol="BOLT",
        instrument=Instrument.shares,
        qty=10.0,
        avg_price=first.avg_price,
        stop_price=0.0,
        option_detail={},
        status=PositionStatus.open,
        book=CORE_BOOK,
        opened_at=utcnow(),
    )
    db.add(second)
    db.flush()
    total = first.qty + second.qty

    from midas.tools import market_data

    real_bars = market_data.get_daily_bars

    def falling_bolt(ticker: str, days: int = 60):
        result = real_bars(ticker, days)
        if ticker == "BOLT":
            for i, bar in enumerate(result.data):
                bar["close"] = 100.0 - i
        return result

    monkeypatch.setattr(TOOLS["get_daily_bars"], "fn", falling_bolt)
    plan = core.build_plan(db)
    sells = [o for o in plan.orders if o.side == "sell"]
    assert [o.ticker for o in sells] == ["BOLT"]
    assert sells[0].qty == pytest.approx(total)  # both rows counted

    result = core.run_rebalance(db, force=True)
    db.commit()
    assert result["status"] == "executed"
    rows = db.scalars(select(Position).where(Position.ticker == "BOLT")).all()
    assert len(rows) == 2 and all(r.status == PositionStatus.closed for r in rows)
    sold = db.scalars(select(Order).where(Order.side == OrderSide.sell)).all()
    assert sum(o.qty for o in sold) == pytest.approx(total)
    assert {o.position_id for o in sold} == {first.id, second.id}


# ---------- K2 + K4: stamping, retries, rejections ----------


def test_an_order_that_raises_is_recorded_and_the_month_stays_due(db, monkeypatch):
    real_submit = paper_broker.submit

    def flaky(session, order):
        if order.symbol == "BOLT":
            raise RuntimeError("quote feed down")
        return real_submit(session, order)

    monkeypatch.setattr(paper_broker, "submit", flaky)

    result = core.run_rebalance(db, force=True)
    db.commit()
    assert result["status"] == "failed"
    assert set(result["failed"]) == {"BOLT"}
    assert result["attempts"] == 1
    assert {p.ticker for p in core.core_positions(db)} == {"ACME", "DUNE"}  # the rest filled
    assert core.rebalance_due(db)  # NOT this month's answer yet
    run = db.get(PipelineRun, uuid.UUID(result["run_id"]))
    assert run is not None and run.status == RunStatus.failed and "BOLT" in run.error
    failed = _events(db, "core.failed")
    assert len(failed) == 1 and "BOLT" in failed[0].summary
    assert "RuntimeError" in failed[0].payload["error"]
    # the BOLT order is not left sitting 'approved'
    bolt_orders = db.scalars(select(Order).where(Order.symbol == "BOLT")).all()
    assert bolt_orders and all(o.status == OrderStatus.rejected for o in bolt_orders)
    # K4: a thesis whose first buy never filled is not left approved
    assert _theses(db, "BOLT", ThesisStatus.approved) == []
    assert len(_theses(db, "BOLT", ThesisStatus.rejected)) == 1
    assert _theses(db, "ACME", ThesisStatus.executed)  # a filled slice keeps its thesis

    # second attempt: still retried
    result2 = core.run_rebalance(db, force=True)
    db.commit()
    assert result2["status"] == "failed" and result2["attempts"] == 2
    assert core.rebalance_due(db)
    # third attempt: give up for the month so the pass does not retry daily
    result3 = core.run_rebalance(db, force=True)
    db.commit()
    assert result3["status"] == "failed" and result3["attempts"] == 3
    assert not core.rebalance_due(db)
    assert len(_events(db, "core.failed")) == 3
    counter = db.get(SystemState, core.ATTEMPTS_KEY)
    assert counter is not None and counter.value["attempts"] == 3
    assert _theses(db, "BOLT", ThesisStatus.approved) == []


def test_a_broker_rejected_first_buy_is_this_months_answer(db, monkeypatch):
    real_submit = paper_broker.submit

    def rejecting(session, order):
        if order.symbol == "BOLT":
            order.status = OrderStatus.rejected
            order.error = "insufficient paper cash (100.00 < 20000.00)"
            return order
        return real_submit(session, order)

    monkeypatch.setattr(paper_broker, "submit", rejecting)

    result = core.run_rebalance(db, force=True)
    db.commit()
    assert result["status"] == "executed"
    assert set(result["rejected"]) == {"BOLT"}
    assert not core.rebalance_due(db)  # no daily retries of a cash problem
    assert {p.ticker for p in core.core_positions(db)} == {"ACME", "DUNE"}
    rejected = _events(db, "core.rejected")
    assert len(rejected) == 1 and "BOLT" in rejected[0].summary
    assert "insufficient paper cash" in rejected[0].summary
    assert _events(db, "core.failed") == []
    # K4
    assert _theses(db, "BOLT", ThesisStatus.approved) == []
    assert len(_theses(db, "BOLT", ThesisStatus.rejected)) == 1
    artifact_fills = result["orders"]
    assert any("BOLT: rejected" in o for o in artifact_fills)


def test_a_rejected_top_up_keeps_the_executed_thesis(db, monkeypatch):
    core.run_rebalance(db, force=True)
    db.commit()
    account = paper_broker.get_account(db)
    paper_broker.set_account(db, cash=account["cash"] + 50_000, equity=account["equity"] + 50_000)

    def rejecting(session, order):
        order.status = OrderStatus.rejected
        order.error = "insufficient paper cash"
        return order

    monkeypatch.setattr(paper_broker, "submit", rejecting)
    result = core.run_rebalance(db, force=True)
    db.commit()
    assert result["status"] == "executed" and set(result["rejected"]) == {"ACME", "BOLT", "DUNE"}
    for t in ("ACME", "BOLT", "DUNE"):
        assert len(_theses(db, t, ThesisStatus.executed)) == 1  # a held slice keeps its thesis
        assert _theses(db, t, ThesisStatus.rejected) == []
    assert len(core.core_positions(db)) == 3


def test_nothing_to_do_and_l0_proposal_both_stamp_the_month(db, monkeypatch):
    core.run_rebalance(db, force=True)
    db.commit()
    assert core.run_rebalance(db, force=True)["status"] == "nothing_to_do"
    assert not core.rebalance_due(db)

    # a fresh ledger at L0: the pending approval request is the month's answer
    for row in db.scalars(select(SystemState)).all():
        if row.key == core.LAST_REBALANCE_KEY:
            db.delete(row)
    db.flush()
    assert core.rebalance_due(db)
    for p in core.core_positions(db):
        p.status = PositionStatus.closed
    db.flush()
    monkeypatch.setattr(settings, "autonomy_level", 0)
    result = core.run_rebalance(db)
    db.commit()
    assert result["status"] == "awaiting_approval"
    assert not core.rebalance_due(db)


def test_a_plan_that_cannot_be_built_leaves_the_month_due(db, monkeypatch):
    def no_bars(ticker: str, days: int = 60):
        raise ToolError("no bar data source available")

    monkeypatch.setattr(TOOLS["get_daily_bars"], "fn", no_bars)
    with pytest.raises(ToolError):
        core.run_rebalance(db, force=True)
    assert core.rebalance_due(db)
