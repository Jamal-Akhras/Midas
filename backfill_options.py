"""One-time backfill: place internal-simulator option positions at the REAL
Alpaca paper account and adopt the broker's fill as the position's basis.

Safe by construction: takes the monitor pass lock (no interleaved passes),
is idempotent (skips positions whose entry order is already broker-backed),
and audits every adjustment. Run DURING MARKET HOURS (options have no
extended-hours trading):  python backfill_options.py
"""
import time

import httpx

from midas.audit import record_event
from midas.config import settings
from midas.db import init_db, session_factory
from midas.execution import paper_broker
from midas.models import (
    ActorType, Broker, Instrument, Order, OrderSide, OrderStatus, Position,
    PositionStatus, utcnow,
)
from midas.scheduler.monitor import acquire_pass_lock, release_pass_lock
from sqlalchemy import select

init_db()

with session_factory() as db:
    token = acquire_pass_lock(db)
if token is None:
    print("monitor pass in progress - try again in a minute")
    raise SystemExit(1)

try:
    with session_factory() as db:
        level = paper_broker.alpaca_options_level()
        print(f"alpaca options trading level: {level}")
        opts = list(db.scalars(select(Position).where(
            Position.status == PositionStatus.open,
            Position.instrument == Instrument.option)))
        if not opts:
            print("no open option positions - nothing to backfill")
            raise SystemExit(0)
        for p in opts:
            entry = db.scalar(select(Order).where(
                Order.position_id == p.id, Order.side == OrderSide.buy,
                Order.status == OrderStatus.filled))
            if entry is None:
                entry = db.scalar(select(Order).where(
                    Order.thesis_id == p.thesis_id, Order.side == OrderSide.buy,
                    Order.status == OrderStatus.filled))
            if entry is None:
                print(f"{p.symbol}: no filled entry order found - skipping")
                continue
            if entry.broker == Broker.alpaca_paper:
                print(f"{p.symbol}: already broker-backed - skipping")
                continue
            _, detail = paper_broker._parse_option_symbol(p.symbol)
            needed = 3 if detail.get("short_strike") is not None else 2
            if level is None or level < needed:
                print(f"{p.symbol}: needs options level {needed}, "
                      f"account has {level} - skipping")
                continue
            payload = paper_broker.option_order_payload(entry, p.avg_price)
            if payload is None:
                print(f"{p.symbol}: could not price - skipping")
                continue
            payload["client_order_id"] = f"bf-{p.id.hex[:16]}"
            base = settings.alpaca_paper_base_url
            r = httpx.post(f"{base}/v2/orders", json=payload,
                           headers=paper_broker._alpaca_headers(), timeout=15)
            if r.status_code == 422 and "client_order_id" in r.text:
                r = httpx.get(f"{base}/v2/orders:by_client_order_id",
                              params={"client_order_id": payload["client_order_id"]},
                              headers=paper_broker._alpaca_headers(), timeout=15)
            if r.status_code not in (200, 201):
                print(f"{p.symbol}: alpaca rejected ({r.status_code}): "
                      f"{r.text[:200]}")
                continue
            oid = r.json()["id"]
            fill = None
            for _ in range(20):
                time.sleep(1)
                o = httpx.get(f"{base}/v2/orders/{oid}",
                              headers=paper_broker._alpaca_headers(),
                              timeout=15).json()
                if o.get("status") == "filled":
                    fill = float(o["filled_avg_price"])
                    break
                if o.get("status") in ("canceled", "rejected", "expired"):
                    break
            if fill is None:
                httpx.delete(f"{base}/v2/orders/{oid}",
                             headers=paper_broker._alpaca_headers(), timeout=15)
                print(f"{p.symbol}: no fill within 20s - order cancelled, "
                      f"position stays on the simulator")
                continue
            old_basis = p.avg_price
            mult = 100
            cash_delta = round((old_basis - fill) * p.qty * mult, 2)
            account = paper_broker.get_account(db)
            paper_broker.set_account(db, cash=account["cash"] + cash_delta,
                                     equity=account["equity"] + cash_delta)
            p.avg_price = fill
            entry.broker = Broker.alpaca_paper
            entry.alpaca_order_id = oid
            entry.filled_price = fill
            record_event(
                db, event_type="position.backfilled",
                summary=(f"{p.symbol}: re-based to Alpaca fill {fill} "
                         f"(was sim {old_basis}); cash adjusted "
                         f"{cash_delta:+.2f}"),
                actor_type=ActorType.operator, subject_type="position",
                subject_id=p.id,
                payload={"alpaca_order_id": oid, "old_basis": old_basis,
                         "new_basis": fill, "cash_delta": cash_delta},
            )
            db.commit()
            print(f"{p.symbol}: backfilled @ {fill} "
                  f"(sim basis was {old_basis}, cash {cash_delta:+.2f})")
finally:
    with session_factory() as db:
        release_pass_lock(db, token)
print("done")
