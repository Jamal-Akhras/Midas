"""The ledger must hold what the broker holds. Fractional share orders go to
Alpaca rounded to three decimals, so the quantity Alpaca fills can differ from
the quantity the book planned; the ledger adopts the broker's quantity, or
every later reconciliation flags a fill the machine itself produced (the first
core rebalance of November 2026 did exactly that: 1.156627 planned, 1.157 filled)."""

import httpx

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
    ResearchArtifact,
    RunKind,
    ThesisStatus,
    TradeThesis,
)


def _thesis(db, ticker="SPY"):
    run = PipelineRun(kind=RunKind.committee, ticker=ticker)
    db.add(run)
    db.flush()
    art = ResearchArtifact(
        run_id=run.id, ticker=ticker, agent_slug="investor", artifact_type="verdict", content={}
    )
    db.add(art)
    db.flush()
    thesis = TradeThesis(
        run_id=run.id,
        verdict_artifact_id=art.id,
        ticker=ticker,
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=700,
        entry_zone_high=760,
        stop_price=650,
        status=ThesisStatus.approved,
    )
    db.add(thesis)
    db.flush()
    return thesis


def _order(db, thesis, qty):
    order = Order(
        thesis_id=thesis.id,
        side=OrderSide.buy,
        instrument=Instrument.shares,
        symbol=thesis.ticker,
        qty=qty,
        reason=OrderReason.entry,
        status=OrderStatus.approved,
    )
    db.add(order)
    db.flush()
    return order


def _mirror_on(monkeypatch):
    monkeypatch.setattr(settings, "alpaca_api_key", "k")
    monkeypatch.setattr(settings, "alpaca_api_secret", "s")
    monkeypatch.setattr(settings, "alpaca_mirror_orders", True)


def test_mirror_reports_the_quantity_alpaca_filled(db, monkeypatch):
    _mirror_on(monkeypatch)
    monkeypatch.setattr("time.sleep", lambda s: None)
    sent: list[dict] = []

    def fake_post(url, *, json=None, headers=None, timeout=None):
        sent.append(json)
        return httpx.Response(200, json={"id": "alp-1"}, request=httpx.Request("POST", url))

    def fake_get(url, *, headers=None, timeout=None):
        body = {"status": "filled", "filled_avg_price": "744.048", "filled_qty": "0.161"}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)

    order = _order(db, _thesis(db), qty=0.161249)
    assert pb._mirror_to_alpaca(order) == (744.048, "alp-1", 0.161)
    assert sent[0]["qty"] == "0.161"  # Alpaca's fractional precision, not ours


def test_ledger_adopts_the_broker_quantity(db, monkeypatch):
    _mirror_on(monkeypatch)
    monkeypatch.setattr(pb, "mark_price", lambda db, o: 744.0)
    monkeypatch.setattr(pb, "_mirror_to_alpaca", lambda o: (744.048, "alp-1", 0.161))
    thesis = _thesis(db)
    cash_before = pb.get_account(db)["cash"]

    order = _order(db, thesis, qty=0.161249)
    pb.submit(db, order)

    assert order.status == OrderStatus.filled
    assert order.qty == 0.161  # the fill is the fact; the plan was the intent
    assert order.filled_price == 744.048
    # account cash is kept to the cent; the debit must be the broker quantity times price
    assert abs(pb.get_account(db)["cash"] - (cash_before - 0.161 * 744.048)) < 0.005

    position = pb.apply_fill_to_position(db, order, thesis)
    assert position.qty == 0.161

    record = {
        "client_order_id": str(order.id),
        "filled_qty": "0.161",
        "filled_avg_price": "744.048",
    }
    result = pb.reconcile_mirrored_orders(db, alpaca_orders=[record])
    assert result["matched"] == 1 and result["mismatched"] == 0

    from sqlalchemy import select

    from midas.models import AuditEvent

    filled = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "order.filled"))
    assert filled is not None and filled.payload["requested_qty"] == 0.161249


def test_exact_fill_leaves_the_quantity_alone(db, monkeypatch):
    _mirror_on(monkeypatch)
    monkeypatch.setattr(pb, "mark_price", lambda db, o: 100.0)
    monkeypatch.setattr(pb, "_mirror_to_alpaca", lambda o: (100.5, "alp-2", 10.0))
    order = _order(db, _thesis(db, "ORCL"), qty=10)
    pb.submit(db, order)
    assert order.qty == 10 and order.filled_price == 100.5
