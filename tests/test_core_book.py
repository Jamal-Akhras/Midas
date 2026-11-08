"""The core book: pure rules, plan arithmetic, and the ladder path in demo
mode. Core positions must be invisible to the swing committee's machinery."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.execution import executor
from midas.models import (
    CORE_BOOK,
    ApprovalRequest,
    ApprovalStatus,
    ArtifactType,
    EventFlag,
    Order,
    OrderStatus,
    Position,
    PositionStatus,
    ResearchArtifact,
    utcnow,
)
from midas.outcomes.scoring import score_closed_positions, stats
from midas.risk import engine as risk_engine
from midas.scheduler import monitor
from midas.strategy import core
from midas.strategy.core import plan_orders, trend_targets


@pytest.fixture(autouse=True)
def core_settings(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)
    monkeypatch.setattr(settings, "core_enabled", True)
    monkeypatch.setattr(settings, "core_universe", "ACME,BOLT,DUNE")
    monkeypatch.setattr(settings, "core_allocation_pct", 60.0)
    monkeypatch.setattr(settings, "core_trend_window", 20)
    monkeypatch.setattr(settings, "fractional_shares", True)


# ---------- pure rules ----------


def test_trend_targets_equal_slices_only_above_the_average():
    rising = [100 + i for i in range(30)]
    falling = [130 - i for i in range(30)]
    short = [100.0] * 5
    targets, detail = trend_targets({"A": rising, "B": falling, "C": short}, window=20)
    assert targets == {"A": pytest.approx(1 / 3), "B": 0.0, "C": 0.0}
    assert detail["A"]["above"] is True and detail["B"]["above"] is False
    assert "fewer than 20 bars" in detail["C"]["note"]
    assert trend_targets({}, 20) == ({}, {})


def test_plan_orders_moves_holdings_to_target_sells_first():
    orders = plan_orders(
        equity=100_000,
        allocation_pct=60,
        targets={"A": 0.5, "B": 0.5},
        prices={"A": 100.0, "B": 50.0, "C": 20.0},
        holdings={"C": 10.0},
    )
    assert [(o.side, o.ticker, o.qty) for o in orders] == [
        ("sell", "C", 10.0),
        ("buy", "A", 300.0),
        ("buy", "B", 600.0),
    ]
    assert orders[1].target_weight == 0.5 and orders[1].current_weight == 0.0


def test_plan_orders_skips_tiny_trades_and_respects_whole_shares():
    orders = plan_orders(
        equity=100_000,
        allocation_pct=60,
        targets={"A": 0.5},
        prices={"A": 100.0},
        holdings={"A": 299.9},  # $10 away from target: below the minimum trade
    )
    assert orders == []
    orders = plan_orders(
        equity=10_000,
        allocation_pct=50,
        targets={"A": 1.0},
        prices={"A": 333.0},
        holdings={},
        fractional=False,
    )
    assert orders[0].qty == 15.0  # 5000 / 333 = 15.01 -> whole shares


def test_plan_orders_never_sells_more_than_held():
    orders = plan_orders(
        equity=1_000,
        allocation_pct=100,
        targets={"A": 0.0},
        prices={"A": 10.0},
        holdings={"A": 5.0},
    )
    assert orders == [] or orders[0].qty <= 5.0


# ---------- the ladder, in demo mode ----------


def _pending_core_plan(db) -> ApprovalRequest:
    req = db.scalar(
        select(ApprovalRequest).where(
            ApprovalRequest.subject_type == "core_plan",
            ApprovalRequest.status == ApprovalStatus.pending,
        )
    )
    assert req is not None
    return req


def test_l0_rebalance_proposes_a_plan_and_approval_executes_it(db):
    result = core.run_rebalance(db, force=True)
    db.commit()
    assert result["status"] == "awaiting_approval"
    plan = db.get(ResearchArtifact, __import__("uuid").UUID(result["artifact_id"]))
    assert plan is not None and plan.artifact_type == ArtifactType.core_plan
    assert set(plan.content["targets"]) == {"ACME", "BOLT", "DUNE"}
    assert all(w == pytest.approx(1 / 3) for w in plan.content["targets"].values())
    assert db.scalars(select(Position)).all() == []  # nothing traded yet at L0

    req = _pending_core_plan(db)
    executor.resolve_approval(db, req, approve=True)
    db.commit()

    positions = db.scalars(select(Position).where(Position.status == PositionStatus.open)).all()
    assert {p.ticker for p in positions} == {"ACME", "BOLT", "DUNE"}
    assert all(p.book == CORE_BOOK and p.stop_price == 0.0 for p in positions)
    orders = db.scalars(select(Order)).all()
    assert len(orders) == 3 and all(o.status == OrderStatus.filled for o in orders)
    deployed = sum(o.filled_price * o.qty for o in orders)
    assert deployed == pytest.approx(60_000, rel=0.02)
    # the whole plan is auditable from the artifact
    assert len(plan.content["fills"]) == 3


def test_l1_rebalance_executes_without_an_approval(db, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 1)
    result = core.run_rebalance(db, force=True)
    db.commit()
    assert result["status"] == "executed" and len(result["orders"]) == 3
    assert db.scalar(select(ApprovalRequest)) is None
    assert len(core.core_positions(db)) == 3


def test_rebalance_is_monthly_unless_forced(db, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 1)
    assert core.rebalance_due(db)
    core.run_rebalance(db, force=True)
    db.commit()
    assert not core.rebalance_due(db)
    assert core.run_rebalance(db)["status"] == "not_due"
    monkeypatch.setattr(settings, "core_enabled", False)
    assert core.run_rebalance(db)["status"] == "disabled"


def test_core_positions_are_invisible_to_the_swing_machinery(db, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 1)
    core.run_rebalance(db, force=True)
    db.commit()
    assert risk_engine.open_positions(db) == []  # swing caps see an empty book
    assert len(risk_engine.open_positions(db, book=None)) == 3
    assert risk_engine.open_notional_usd(db) == 0.0
    assert monitor.due_positions(db) == []  # no reviews, no stop checks
    for p in core.core_positions(db):  # age them past the time stop
        p.opened_at = utcnow() - timedelta(days=400)
    db.flush()
    assert monitor.apply_time_stops(db) == []
    assert monitor.ratchet_stops(db) == []
    s = stats(db)
    assert s["closed_positions"] == 0


def test_high_severity_negative_flag_vetoes_a_slice(db):
    db.add(
        EventFlag(
            ticker="BOLT",
            kind="guidance_cut",
            severity="high",
            direction="negative",
            summary="FY guidance cut 20%",
            source_url="https://example.com/8k",
            price_at_flag=60.0,
        )
    )
    db.add(
        EventFlag(
            ticker="DUNE",
            kind="litigation",
            severity="low",
            direction="negative",
            summary="minor suit",
            source_url="https://example.com/x",
            price_at_flag=90.0,
        )
    )
    stale = EventFlag(
        ticker="ACME",
        kind="accounting",
        severity="high",
        direction="negative",
        summary="old restatement",
        source_url="https://example.com/old",
        price_at_flag=180.0,
    )
    db.add(stale)
    db.flush()
    stale.created_at = utcnow() - timedelta(days=90)
    db.flush()
    plan = core.build_plan(db)
    assert set(plan.vetoes) == {"BOLT"}
    assert plan.targets["BOLT"] == 0.0 and plan.targets["ACME"] > 0 and plan.targets["DUNE"] > 0
    assert plan.trend["BOLT"]["veto"].startswith("guidance_cut")
    assert {o.ticker for o in plan.orders} == {"ACME", "DUNE"}


def test_trend_break_sells_the_slice_and_the_outcome_stays_out_of_swing_stats(db, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 1)
    core.run_rebalance(db, force=True)
    db.commit()
    from midas.tools import market_data

    real_bars = market_data.get_daily_bars

    def falling_bolt(ticker: str, days: int = 60):
        result = real_bars(ticker, days)
        if ticker == "BOLT":
            for i, bar in enumerate(result.data):  # monotonically falling closes
                bar["close"] = 100.0 - i
        return result

    from midas.tools.registry import TOOLS

    monkeypatch.setattr(market_data, "get_daily_bars", falling_bolt)
    monkeypatch.setattr(TOOLS["get_daily_bars"], "fn", falling_bolt)
    plan = core.build_plan(db)
    assert plan.targets["BOLT"] == 0.0
    sells = [o for o in plan.orders if o.side == "sell"]
    assert [o.ticker for o in sells] == ["BOLT"]
    result = core.run_rebalance(db, force=True)
    db.commit()
    assert result["status"] == "executed"
    bolt = db.scalar(select(Position).where(Position.ticker == "BOLT"))
    assert bolt.status == PositionStatus.closed
    scored = score_closed_positions(db)
    assert len(scored) == 1
    assert stats(db)["closed_positions"] == 0  # core outcomes never count as committee evidence


def test_denied_plan_is_this_months_answer(db):
    core.run_rebalance(db, force=True)
    db.commit()
    req = _pending_core_plan(db)
    executor.resolve_approval(db, req, approve=False, note="not this month")
    db.commit()
    assert req.status == ApprovalStatus.denied
    assert db.scalars(select(Position)).all() == []
    assert core.run_rebalance(db)["status"] == "not_due"
