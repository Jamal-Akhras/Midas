"""Execution integrity: the ledger's mirrored fills must match Alpaca's
records exactly - unattended runs test execution as much as decisions."""
import uuid

import pytest
from sqlalchemy import select

from midas.execution.paper_broker import reconcile_mirrored_orders
from midas.models import (
    ArtifactType, AuditEvent, Broker, Direction, Instrument, Order,
    OrderReason, OrderSide, OrderStatus, PipelineRun, ResearchArtifact,
    RunKind, ThesisStatus, TradeThesis, utcnow,
)


def mirrored_order(db, qty=10.0, price=100.0):
    run = PipelineRun(kind=RunKind.committee, ticker="ORCL")
    db.add(run)
    db.flush()
    art = ResearchArtifact(run_id=run.id, ticker="ORCL", agent_slug="investor",
                           artifact_type=ArtifactType.verdict, content={})
    db.add(art)
    db.flush()
    thesis = TradeThesis(run_id=run.id, verdict_artifact_id=art.id,
                         ticker="ORCL", direction=Direction.bullish,
                         instrument=Instrument.shares, entry_zone_low=99,
                         entry_zone_high=101, stop_price=95,
                         status=ThesisStatus.executed)
    db.add(thesis)
    db.flush()
    o = Order(thesis_id=thesis.id, side=OrderSide.buy,
              instrument=Instrument.shares, symbol="ORCL", qty=qty,
              reason=OrderReason.entry, status=OrderStatus.filled,
              broker=Broker.alpaca_paper, filled_price=price,
              filled_at=utcnow())
    db.add(o)
    db.flush()
    return o


def alpaca_record(o, qty=None, price=None):
    return {"client_order_id": str(o.id),
            "filled_qty": str(qty if qty is not None else o.qty),
            "filled_avg_price": str(price if price is not None else o.filled_price)}


def test_matching_fills_reconcile_clean(db):
    o = mirrored_order(db)
    result = reconcile_mirrored_orders(db, alpaca_orders=[alpaca_record(o)])
    assert result == {"checked": 1, "matched": 1, "mismatched": 0,
                      "unverifiable": 0}


def test_price_drift_raises_loud_audit_event(db):
    o = mirrored_order(db, price=100.0)
    result = reconcile_mirrored_orders(
        db, alpaca_orders=[alpaca_record(o, price=101.50)])
    assert result["mismatched"] == 1
    evt = db.scalar(select(AuditEvent).where(
        AuditEvent.event_type == "execution.mismatch"))
    assert evt is not None and "MIRROR MISMATCH" in evt.summary


def test_qty_drift_detected(db):
    o = mirrored_order(db, qty=10)
    result = reconcile_mirrored_orders(db, alpaca_orders=[alpaca_record(o, qty=9)])
    assert result["mismatched"] == 1


def test_missing_record_is_unverifiable_not_mismatch(db):
    mirrored_order(db)
    result = reconcile_mirrored_orders(db, alpaca_orders=[])
    assert result["unverifiable"] == 1 and result["mismatched"] == 0


def test_sim_fills_are_not_checked(db):
    o = mirrored_order(db)
    o.broker = Broker.paper_sim
    db.flush()
    result = reconcile_mirrored_orders(db, alpaca_orders=[])
    assert result["checked"] == 0
