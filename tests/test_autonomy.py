"""The autonomy ladder: config-capped, code-enforced. Levels 3+ (real
money) must be unrepresentable in this codebase."""

import pytest
from sqlalchemy import select

from midas.config import Settings, settings
from midas.models import (
    ApprovalRequest,
    Broker,
    Order,
    OrderStatus,
    Position,
    PositionStatus,
    ThesisStatus,
    TradeThesis,
)
from midas.pipeline.committee import run_committee
from midas.scheduler.monitor import run_monitor_once


@pytest.fixture(autouse=True)
def demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)


def test_autonomy_above_2_is_rejected_at_config_time():
    with pytest.raises(ValueError, match="real-order code path"):
        Settings(autonomy_level=3)
    with pytest.raises(ValueError):
        Settings(autonomy_level=4)


def test_no_real_broker_exists():
    values = {b.value for b in Broker}
    assert values == {"paper_sim", "alpaca_paper"}  # nothing touches real money


def test_l0_requires_operator_approval(factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 0)
    run_committee(factory, demo_router, "ACME")
    with factory() as db:
        thesis = db.scalar(select(TradeThesis))
        assert thesis.status == ThesisStatus.proposed
        assert db.scalar(select(Order)) is None  # nothing executed
        assert db.scalar(select(ApprovalRequest)) is not None


def test_l1_auto_executes_entry_but_proposes_exits(factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 1)
    run_committee(factory, demo_router, "ACME")
    with factory() as db:
        thesis = db.scalar(select(TradeThesis))
        assert thesis.status == ThesisStatus.approved  # auto-approved, unexecuted
        assert db.scalar(select(ApprovalRequest)) is None

    summary = run_monitor_once(factory, demo_router)
    assert summary["executed"] == 1
    with factory() as db:
        order = db.scalar(select(Order))
        assert order.status == OrderStatus.filled
        assert order.approved_by == "investor_agent(L1)"
        # exits at L1 still go through the operator
        from midas.execution.executor import propose_or_execute_exit

        position = db.scalar(select(Position))
        result = propose_or_execute_exit(db, position, decision="exit", reasons=["test"])
        assert isinstance(result, ApprovalRequest)


def test_l2_auto_executes_exits(factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 2)
    run_committee(factory, demo_router, "ACME")
    run_monitor_once(factory, demo_router)  # executes the entry
    with factory() as db:
        from midas.execution.executor import propose_or_execute_exit

        position = db.scalar(select(Position).where(Position.status == PositionStatus.open))
        result = propose_or_execute_exit(db, position, decision="exit", reasons=["test"])
        db.commit()
        assert isinstance(result, Order)
        assert result.status == OrderStatus.filled
        assert result.approved_by == "investor_agent(L2)"
