"""Paper broker: a built-in simulator (default) with the same interface an
Alpaca-paper adapter would implement. Fills at the current tool-sourced
price; every fill is a snapshot-backed, audited event.

There is NO real-money broker in this codebase. That is non-negotiable #1:
the real-order code path does not exist until the L3 statistical bar is met.
"""
import logging
from typing import Any

from sqlalchemy.orm import Session

from midas.audit import record_event
from midas.models import (
    ActorType,
    Broker,
    Instrument,
    Order,
    OrderReason,
    OrderSide,
    OrderStatus,
    Position,
    PositionStatus,
    SystemState,
    TradeThesis,
    utcnow,
)
from midas.tools.base import ToolContext
from midas.tools.registry import TOOLS

logger = logging.getLogger(__name__)

ACCOUNT_KEY = "paper_account"
def starting_cash() -> float:
    from midas.config import settings

    return settings.paper_starting_cash


def get_account(db: Session) -> dict[str, Any]:
    row = db.get(SystemState, ACCOUNT_KEY)
    if row is None:
        row = SystemState(key=ACCOUNT_KEY,
                          value={"cash": starting_cash(),
                                 "equity": starting_cash()})
        db.add(row)
        db.flush()
    return dict(row.value)


def set_account(db: Session, *, cash: float, equity: float) -> None:
    row = db.get(SystemState, ACCOUNT_KEY)
    value = {"cash": round(cash, 2), "equity": round(equity, 2)}
    if row is None:
        db.add(SystemState(key=ACCOUNT_KEY, value=value))
    else:
        row.value = value
    db.flush()


def mark_price(db: Session, order: Order) -> float:
    """Current fill price from the market-data tools (snapshotted).
    Options fill at chain mid for the exact contract."""
    ctx = ToolContext(db, ticker=order.symbol.split("_")[0])
    execute = ctx.executor(TOOLS)
    if order.instrument == Instrument.shares:
        quote = execute("get_quote", {"ticker": order.symbol})["data"]
        return float(quote["price"])
    underlying, detail = _parse_option_symbol(order.symbol)
    chain = execute("get_options_chain", {"ticker": underlying})["data"]

    def mid_of(strike: float) -> float:
        for c in chain:
            if (c["expiry"] == detail["expiry"] and c["type"] == detail["type"]
                    and float(c["strike"]) == strike):
                return float(c["mid"])
        raise LookupError(f"contract {order.symbol} not found in current chain")

    price = mid_of(detail["strike"])
    if detail.get("short_strike") is not None:
        # debit spread marked at net debit (long mid - short mid)
        price = max(price - mid_of(detail["short_strike"]), 0.01)
    return round(price, 4)


def occ_symbol(underlying: str, expiry_iso: str, opt_type: str,
               strike: float) -> str:
    """OCC option symbol: AAPL 2025-01-17 call 190.0 -> AAPL250117C00190000."""
    y, m, d = expiry_iso.split("-")
    cp = "C" if opt_type == "call" else "P"
    return f"{underlying.upper()}{y[2:]}{m}{d}{cp}{int(round(strike * 1000)):08d}"


_options_level_cache: dict = {"at": 0.0, "level": None}


def alpaca_options_level() -> int | None:
    """The paper account's options trading level (cached 10 min).
    Spreads need Level 3; long singles need Level 2."""
    import time as _time

    import httpx

    from midas.config import settings

    if _time.monotonic() - _options_level_cache["at"] < 600:
        return _options_level_cache["level"]
    level = None
    if settings.alpaca_api_key and settings.alpaca_api_secret:
        try:
            r = httpx.get(f"{settings.alpaca_paper_base_url}/v2/account",
                          headers=_alpaca_headers(), timeout=15)
            if r.status_code == 200:
                level = int(r.json().get("options_trading_level") or 0)
        except Exception as exc:
            logger.warning("options level check failed: %s", type(exc).__name__)
    _options_level_cache.update(at=_time.monotonic(), level=level)
    return level


def option_order_payload(order: Order, net_mid: float) -> dict | None:
    """Alpaca order payload for an internal option order: plain limit for
    single legs, order_class=mleg for debit spreads (entry: buy_to_open long
    + sell_to_open short; exit: the *_to_close inverse). None if unpriceable."""
    underlying, detail = _parse_option_symbol(order.symbol)
    entry = order.side == OrderSide.buy
    limit = round(net_mid * (1.02 if entry else 0.98), 2)
    if limit <= 0:
        return None
    long_sym = occ_symbol(underlying, detail["expiry"], detail["type"],
                          detail["strike"])
    base = {"qty": str(int(order.qty)), "type": "limit",
            "limit_price": f"{limit:.2f}", "time_in_force": "day",
            "client_order_id": str(order.id)}
    if detail.get("short_strike") is None:
        return {**base, "symbol": long_sym,
                "side": "buy" if entry else "sell"}
    short_sym = occ_symbol(underlying, detail["expiry"], detail["type"],
                           detail["short_strike"])
    if entry:
        legs = [{"symbol": long_sym, "ratio_qty": "1", "side": "buy",
                 "position_intent": "buy_to_open"},
                {"symbol": short_sym, "ratio_qty": "1", "side": "sell",
                 "position_intent": "sell_to_open"}]
    else:
        legs = [{"symbol": long_sym, "ratio_qty": "1", "side": "sell",
                 "position_intent": "sell_to_close"},
                {"symbol": short_sym, "ratio_qty": "1", "side": "buy",
                 "position_intent": "buy_to_close"}]
    return {**base, "order_class": "mleg", "legs": legs}


def _mirror_option_to_alpaca(order: Order, net_mid: float) -> tuple[float, str] | None:
    """Submit an option order (single or mleg spread) to the REAL Alpaca
    paper account. Returns (net fill price per contract, alpaca_order_id)
    or None - the caller then fails closed or simulates per config."""
    import time as _time

    import httpx

    from midas.config import settings

    if not (settings.alpaca_api_key and settings.alpaca_api_secret
            and settings.alpaca_mirror_orders and settings.alpaca_mirror_options):
        return None
    _, detail = _parse_option_symbol(order.symbol)
    needed = 3 if detail.get("short_strike") is not None else 2
    level = alpaca_options_level()
    if level is None or level < needed:
        logger.warning("options mirror needs level %s, account has %s; sim used",
                       needed, level)
        return None
    payload = option_order_payload(order, net_mid)
    if payload is None:
        return None
    base = settings.alpaca_paper_base_url
    try:
        r = httpx.post(f"{base}/v2/orders", json=payload,
                       headers=_alpaca_headers(), timeout=15)
        if r.status_code not in (200, 201):
            logger.warning("alpaca option mirror rejected (%s): %s",
                           r.status_code, r.text[:200])
            return None
        alpaca_id = r.json()["id"]
        for _ in range(8):
            _time.sleep(1)
            o = httpx.get(f"{base}/v2/orders/{alpaca_id}",
                          headers=_alpaca_headers(), timeout=15).json()
            status = o.get("status")
            if status == "filled":
                return float(o["filled_avg_price"]), alpaca_id
            if status in ("canceled", "rejected", "expired"):
                logger.warning("alpaca option order %s: %s", alpaca_id, status)
                return None
        httpx.delete(f"{base}/v2/orders/{alpaca_id}",
                     headers=_alpaca_headers(), timeout=15)
        logger.warning("alpaca option order %s unfilled; cancelled", alpaca_id)
        return None
    except Exception as exc:
        logger.warning("alpaca option mirror failed: %s", type(exc).__name__)
        return None


def option_symbol(underlying: str, expiry: str, opt_type: str, strike: float,
                  short_strike: float | None = None) -> str:
    """Readable internal option symbol: TICKER_YYYY-MM-DD_call_185.0 (long)
    or ..._185.0x200.0 (debit spread). Human-auditable; swap for OCC format
    when a real chain needs it."""
    tail = f"{strike}" if short_strike is None else f"{strike}x{short_strike}"
    return f"{underlying}_{expiry}_{opt_type}_{tail}"


def _parse_option_symbol(symbol: str) -> tuple[str, dict]:
    underlying, expiry, opt_type, strikes = symbol.split("_")
    if "x" in strikes:
        long_s, short_s = strikes.split("x")
        return underlying, {"expiry": expiry, "type": opt_type,
                            "strike": float(long_s), "short_strike": float(short_s)}
    return underlying, {"expiry": expiry, "type": opt_type,
                        "strike": float(strikes), "short_strike": None}


def _alpaca_headers() -> dict:
    from midas.config import settings

    return {"APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret}


def _mirror_to_alpaca(order: Order) -> tuple[float, str] | None:
    """Submit an equity order to the REAL Alpaca paper account and wait
    briefly for the fill. Returns (fill_price, alpaca_order_id) or None
    (market closed, rejected, unreachable) - the caller then falls back to
    the internal simulator. Paper account only; this module has no concept
    of a real-money endpoint."""
    import time as _time

    import httpx

    from midas.config import settings

    if not (settings.alpaca_api_key and settings.alpaca_api_secret
            and settings.alpaca_mirror_orders):
        return None
    if order.instrument != Instrument.shares:
        return None  # options stay on the internal simulator
    fractional = abs(order.qty - round(order.qty)) > 1e-9
    payload = {
        "symbol": order.symbol,
        "qty": str(int(order.qty)) if not fractional else str(round(order.qty, 3)),
        "side": order.side.value, "time_in_force": "day",
        # fractional orders are market-only at Alpaca; the internal limit
        # check against the entry zone still gates BEFORE we get here
        "type": ("limit" if order.limit_price is not None and not fractional
                 else "market"),
        "client_order_id": str(order.id),  # exact reconciliation key
    }
    if order.limit_price is not None and not fractional:
        payload["limit_price"] = str(round(order.limit_price, 2))
    base = settings.alpaca_paper_base_url
    try:
        r = httpx.post(f"{base}/v2/orders", json=payload,
                       headers=_alpaca_headers(), timeout=15)
        if r.status_code not in (200, 201):
            logger.warning("alpaca mirror rejected (%s): %s",
                           r.status_code, r.text[:200])
            return None
        alpaca_id = r.json()["id"]
        for _ in range(8):
            _time.sleep(1)
            o = httpx.get(f"{base}/v2/orders/{alpaca_id}",
                          headers=_alpaca_headers(), timeout=15).json()
            status = o.get("status")
            if status == "filled":
                return float(o["filled_avg_price"]), alpaca_id
            if status in ("canceled", "rejected", "expired"):
                logger.warning("alpaca mirror order %s: %s", alpaca_id, status)
                return None
        # not filled quickly (market closed / illiquid): cancel and simulate
        httpx.delete(f"{base}/v2/orders/{alpaca_id}",
                     headers=_alpaca_headers(), timeout=15)
        logger.warning("alpaca mirror order %s unfilled; cancelled, sim fill used",
                       alpaca_id)
        return None
    except Exception as exc:
        logger.warning("alpaca mirror failed: %s", type(exc).__name__)
        return None


def effective_stop(db: Session, position: Position) -> float | None:
    """The live stop: the position's (possibly ratcheted) stop, falling
    back to the thesis stop for positions opened before ratcheting existed."""
    if position.stop_price:
        return position.stop_price
    thesis = db.get(TradeThesis, position.thesis_id)
    return thesis.stop_price if thesis else None


def _cancel_standing_stop(db: Session, position_id) -> bool | None:
    """Cancel the resting Alpaca stop before any other sell of the same
    shares - Alpaca holds shares against open orders, so a mirrored sell
    would otherwise be rejected (or worse, both could fill)."""
    import httpx
    from sqlalchemy import select as _select

    from midas.config import settings
    from midas.models import StandingStop

    row = db.scalar(_select(StandingStop).where(
        StandingStop.position_id == position_id, StandingStop.active))
    if row is None:
        return False
    confirmed = False
    try:
        r = httpx.delete(
            f"{settings.alpaca_paper_base_url}/v2/orders/{row.alpaca_order_id}",
            headers=_alpaca_headers(), timeout=15)
        if r.status_code in (200, 204, 404):
            confirmed = True  # cancelled, or unknown to the broker
        else:
            # 422 etc: order not cancelable - check whether it is terminal
            o = httpx.get(
                f"{settings.alpaca_paper_base_url}/v2/orders/{row.alpaca_order_id}",
                headers=_alpaca_headers(), timeout=15).json()
            if o.get("status") in ("canceled", "expired", "rejected", "filled"):
                confirmed = True  # already dead (a fill is booked by sync)
    except Exception as exc:
        logger.warning("standing stop cancel unconfirmed: %s", type(exc).__name__)
    if not confirmed:
        return None  # stop may still be LIVE - the caller must abort the exit
    row.active = False
    db.flush()
    return True


def _restore_standing_stop(db: Session, position_id) -> None:
    """An exit failed AFTER its standing stop was cancelled. An open
    position without its stop is an operational emergency: put the stop
    back NOW, and if that also fails, scream in the audit log."""
    import httpx
    from sqlalchemy import select as _select

    from midas.config import settings
    from midas.models import StandingStop

    position = db.get(Position, position_id)
    if position is None or position.status != PositionStatus.open:
        return
    stop = effective_stop(db, position)
    row = db.scalar(_select(StandingStop).where(
        StandingStop.position_id == position_id))
    ok = False
    if stop and stop > 0:
        cid = f"ssr-{position_id.hex[:16]}-{int(utcnow().timestamp())}"
        try:
            r = httpx.post(
                f"{settings.alpaca_paper_base_url}/v2/orders",
                json={"symbol": position.symbol, "qty": str(int(position.qty)),
                      "side": "sell", "type": "stop",
                      "stop_price": str(round(stop, 2)),
                      "time_in_force": "gtc", "client_order_id": cid},
                headers=_alpaca_headers(), timeout=15)
            if r.status_code in (200, 201):
                oid = r.json()["id"]
                if row is None:
                    db.add(StandingStop(position_id=position_id,
                                        alpaca_order_id=oid,
                                        stop_price=round(stop, 2),
                                        qty=position.qty))
                else:
                    row.alpaca_order_id, row.stop_price = oid, round(stop, 2)
                    row.qty, row.active = position.qty, True
                ok = True
        except Exception as exc:
            logger.warning("standing stop restore failed: %s", type(exc).__name__)
    record_event(
        db, event_type="stop.restored" if ok else "ops.alert",
        summary=(f"{position.symbol}: standing stop re-placed after failed exit"
                 if ok else
                 f"{position.symbol}: OPEN POSITION WITHOUT STANDING STOP - "
                 f"exit failed and stop restore also failed; intervene"),
        actor_type=ActorType.system, subject_type="position",
        subject_id=position_id,
    )
    db.flush()


def _close_from_standing_stop(db: Session, position: Position, price: float,
                              row) -> None:
    """Alpaca's resting stop filled while we weren't looking: book the
    exit into the ledger at Alpaca's fill price. No new order is sent."""
    thesis = db.get(TradeThesis, position.thesis_id)
    order = Order(
        thesis_id=position.thesis_id, position_id=position.id,
        side=OrderSide.sell, instrument=position.instrument,
        symbol=position.symbol, qty=position.qty, reason=OrderReason.exit,
        approved_by="standing_stop(alpaca)", status=OrderStatus.filled,
        filled_price=price, broker=Broker.alpaca_paper,
    )
    order.filled_at = utcnow()
    db.add(order)
    db.flush()
    account = get_account(db)
    set_account(db, cash=account["cash"] + price * position.qty,
                equity=account["equity"])
    row.active = False
    record_event(
        db, event_type="stop.standing_filled",
        summary=f"{position.symbol}: Alpaca standing stop filled @ {price}",
        actor_type=ActorType.system, subject_type="position",
        subject_id=position.id, payload={"price": price, "qty": position.qty},
    )
    apply_fill_to_position(db, order, thesis)


def sync_standing_stops(db: Session) -> dict:
    """Ensure every open shares position has a GTC stop order resting at
    the real Alpaca paper account at its current effective stop. Detects
    stops that filled while the monitor was down and books those exits."""
    import httpx
    from sqlalchemy import select as _select

    from midas.config import settings
    from midas.models import StandingStop

    if not (settings.alpaca_api_key and settings.alpaca_api_secret and
            settings.alpaca_mirror_orders and settings.alpaca_standing_stops):
        return {"enabled": False}
    base = settings.alpaca_paper_base_url
    summary = {"enabled": True, "placed": 0, "replaced": 0, "filled": 0}
    open_shares = [p for p in db.scalars(_select(Position).where(
        Position.status == PositionStatus.open,
        Position.instrument == Instrument.shares))]
    rows = {r.position_id: r for r in db.scalars(_select(StandingStop))}

    summary["skipped_fractional"] = 0
    for p in open_shares:
        if abs(p.qty - round(p.qty)) > 1e-9:
            # no fractional stop orders at Alpaca: the monitor's stop layer
            # covers these positions (accepted trade-off of notional sizing)
            summary["skipped_fractional"] += 1
            continue
        stop = effective_stop(db, p)
        if not stop or stop <= 0:
            continue
        row = rows.get(p.id)
        if row is not None and row.active:
            try:
                o = httpx.get(f"{base}/v2/orders/{row.alpaca_order_id}",
                              headers=_alpaca_headers(), timeout=15).json()
            except Exception as exc:
                logger.warning("standing stop check failed for %s: %s",
                               p.symbol, type(exc).__name__)
                continue
            status = o.get("status", "")
            if status == "filled":
                _close_from_standing_stop(
                    db, p, float(o.get("filled_avg_price") or stop), row)
                summary["filled"] += 1
                continue
            if status in ("canceled", "rejected", "expired"):
                row.active = False  # re-place below
            elif abs(row.stop_price - stop) < 0.01 and abs(row.qty - p.qty) < 1e-9:
                continue  # resting and correct
            else:
                try:
                    httpx.delete(f"{base}/v2/orders/{row.alpaca_order_id}",
                                 headers=_alpaca_headers(), timeout=15)
                except Exception as exc:
                    logger.warning("standing stop replace-cancel failed: %s",
                                   type(exc).__name__)
                    continue
                row.active = False
                summary["replaced"] += 1
        # deterministic id: if a crash lost our record of a placed stop,
        # Alpaca rejects the duplicate and we ADOPT the original instead
        # of resting a second sell order.
        cid = f"ss-{p.id.hex[:20]}-{int(round(stop * 100))}-{int(p.qty)}"
        payload = {"symbol": p.symbol, "qty": str(int(p.qty)), "side": "sell",
                   "type": "stop", "stop_price": str(round(stop, 2)),
                   "time_in_force": "gtc", "client_order_id": cid}
        try:
            r = httpx.post(f"{base}/v2/orders", json=payload,
                           headers=_alpaca_headers(), timeout=15)
            if r.status_code == 422 and "client_order_id" in r.text:
                r = httpx.get(
                    f"{base}/v2/orders:by_client_order_id",
                    params={"client_order_id": cid},
                    headers=_alpaca_headers(), timeout=15)
        except Exception as exc:
            logger.warning("standing stop place failed for %s: %s",
                           p.symbol, type(exc).__name__)
            continue
        if r.status_code not in (200, 201):
            logger.warning("standing stop rejected for %s (%s): %s",
                           p.symbol, r.status_code, r.text[:200])
            continue
        oid = r.json()["id"]
        if row is None:
            row = StandingStop(position_id=p.id, alpaca_order_id=oid,
                               stop_price=round(stop, 2), qty=p.qty)
            db.add(row)
        else:
            row.alpaca_order_id, row.stop_price = oid, round(stop, 2)
            row.qty, row.active = p.qty, True
        summary["placed"] += 1
        record_event(
            db, event_type="stop.standing_placed",
            summary=f"{p.symbol}: GTC stop resting at Alpaca @ {round(stop, 2)}",
            actor_type=ActorType.system, subject_type="position", subject_id=p.id,
        )
    # positions closed internally: pull their orphaned resting stops
    open_ids = {p.id for p in open_shares}
    for pid, row in rows.items():
        if row.active and pid not in open_ids:
            try:
                httpx.delete(f"{base}/v2/orders/{row.alpaca_order_id}",
                             headers=_alpaca_headers(), timeout=15)
            except Exception:
                pass
            row.active = False
    db.flush()
    return summary


def _broker_fills_required(order: Order) -> bool:
    """Alpaca-configured shares orders must fill AT THE BROKER: a broker
    failure rejects the order (retried next pass) rather than silently
    simulating. Simulated fills would contaminate the L2 evidence."""
    from midas.config import settings

    if not (settings.require_broker_fills and settings.alpaca_mirror_orders
            and settings.alpaca_api_key and settings.alpaca_api_secret):
        return False
    if order.instrument == Instrument.shares:
        return True
    # options fail closed only when their mirror is on AND the account level
    # supports the structure - otherwise the simulator is the honest venue
    if settings.alpaca_mirror_options:
        _, detail = _parse_option_symbol(order.symbol)
        needed = 3 if detail.get("short_strike") is not None else 2
        level = alpaca_options_level()
        return level is not None and level >= needed
    return False


def submit(db: Session, order: Order) -> Order:
    """Fill an order. Shares: at the Alpaca paper broker (fail-closed when
    configured). Options: internal simulator with explicit friction - fills
    through the mid plus per-contract fees, never at the frictionless mid."""
    from midas.config import settings

    price = mark_price(db, order)
    option_mid = price
    fee = 0.0
    if order.instrument == Instrument.option:
        slip = settings.option_slippage_pct / 100
        price = round(price * (1 + slip if order.side == OrderSide.buy else 1 - slip), 4)
        legs = 2 if "x" in order.symbol.rsplit("_", 1)[-1] else 1
        fee = round(settings.option_fee_per_contract * order.qty * legs, 2)
    if order.limit_price is not None and (
        (order.side == OrderSide.buy and price > order.limit_price)
        or (order.side == OrderSide.sell and price < order.limit_price)
    ):
            order.status = OrderStatus.rejected
            order.error = f"market {price} beyond limit {order.limit_price}"
            record_event(db, event_type="order.rejected",
                         summary=f"{order.symbol}: {order.error}",
                         subject_type="order", subject_id=order.id)
            return order

    multiplier = 100 if order.instrument == Instrument.option else 1
    notional = price * order.qty * multiplier
    order.fees_usd = fee
    account = get_account(db)
    if order.side == OrderSide.buy and notional + fee > account["cash"]:
        order.status = OrderStatus.rejected
        order.error = f"insufficient paper cash ({account['cash']:.2f} < {notional:.2f})"
        record_event(db, event_type="order.rejected",
                     summary=f"{order.symbol}: {order.error}",
                     subject_type="order", subject_id=order.id)
        return order

    # Mirror equities to the real Alpaca paper account when configured -
    # Alpaca's actual fill price becomes the ledger price and the Alpaca
    # dashboard becomes an independent record of every trade.
    fill_source = "sim"
    alpaca_id = None
    cancelled_stop: bool | None = False
    if (order.side == OrderSide.sell and order.instrument == Instrument.shares
            and order.position_id):
        cancelled_stop = _cancel_standing_stop(db, order.position_id)
        if cancelled_stop is None:  # noqa: SIM102
            # the resting stop may still be live: selling now could fill
            # BOTH orders and go short - abort, keep the stop, alert
            order.status = OrderStatus.rejected
            order.error = ("standing-stop cancellation unconfirmed; exit "
                           "aborted to avoid a double-sell; will retry")
            record_event(db, event_type="ops.alert",
                         summary=f"{order.symbol}: {order.error}",
                         subject_type="order", subject_id=order.id)
            return order
    if order.instrument == Instrument.option:
        mirrored = _mirror_option_to_alpaca(order, option_mid)
    else:
        mirrored = _mirror_to_alpaca(order)
    if mirrored is not None:
        price, alpaca_id = mirrored
        if order.instrument == Instrument.option:
            fee = 0.0  # real broker fill: no synthetic friction on top
            order.fees_usd = 0.0
        notional = price * order.qty * multiplier
        order.broker = Broker.alpaca_paper
        order.alpaca_order_id = alpaca_id
        fill_source = "alpaca_paper"
    elif _broker_fills_required(order):
        order.status = OrderStatus.rejected
        order.error = ("broker fill unavailable (fail-closed: simulated fills "
                       "must not contaminate the broker-backed record); "
                       "will retry next pass")
        record_event(db, event_type="order.rejected",
                     summary=f"{order.symbol}: {order.error}",
                     subject_type="order", subject_id=order.id)
        if cancelled_stop:
            _restore_standing_stop(db, order.position_id)
        return order

    order.status = OrderStatus.filled
    order.filled_price = price
    order.filled_at = utcnow()
    cash = account["cash"] + (-notional if order.side == OrderSide.buy else notional) - fee
    set_account(db, cash=cash, equity=account["equity"])  # equity refreshed by marks
    record_event(
        db, event_type="order.filled",
        summary=f"{order.side.value} {order.qty:g} {order.symbol} @ {price} "
                f"({fill_source}" + (f", fees ${fee:.2f}" if fee else "") + ")",
        actor_type=ActorType.system, subject_type="order", subject_id=order.id,
        payload={"price": price, "notional": round(notional, 2), "fees_usd": fee,
                 "fill_source": fill_source, "alpaca_order_id": alpaca_id},
    )
    return order


def apply_fill_to_position(db: Session, order: Order, thesis, sector: str = "") -> Position:
    """Update or create the Position for a filled order."""
    assert order.status == OrderStatus.filled and order.filled_price is not None
    position = db.get(Position, order.position_id) if order.position_id else None
    fee = order.fees_usd or 0.0
    multiplier_f = 100 if order.instrument == Instrument.option else 1
    if order.side == OrderSide.buy:
        if position is None:
            position = Position(
                thesis_id=order.thesis_id, ticker=thesis.ticker, symbol=order.symbol,
                instrument=order.instrument, sector=sector, qty=order.qty,
                avg_price=order.filled_price + fee / (order.qty * multiplier_f),
                stop_price=thesis.stop_price,
                option_detail=(_parse_option_symbol(order.symbol)[1]
                               if order.instrument == Instrument.option else {}),
                opened_at=order.filled_at,
            )
            db.add(position)
            db.flush()
        else:
            total = position.qty + order.qty
            position.avg_price = (position.avg_price * position.qty
                                  + order.filled_price * order.qty
                                  + fee / multiplier_f) / total
            position.qty = total
        order.position_id = position.id
        return position

    # sell: reduce/close
    if position is None:
        raise ValueError("sell order without a position")
    multiplier = 100 if order.instrument == Instrument.option else 1
    closed_qty = min(order.qty, position.qty)
    pnl = (order.filled_price - position.avg_price) * closed_qty * multiplier - fee
    position.qty -= closed_qty
    position.realized_pnl_usd += pnl
    if position.qty <= 0:
        position.status = PositionStatus.closed
        position.closed_at = order.filled_at
    record_event(
        db, event_type="position.reduced" if position.qty > 0 else "position.closed",
        summary=f"{position.symbol}: realized {pnl:+.2f} USD (paper)",
        subject_type="position", subject_id=position.id,
        payload={"pnl_usd": round(pnl, 2), "remaining_qty": position.qty},
    )
    return position


def fetch_alpaca_account() -> dict | None:
    """Alpaca's own view of the paper account (equity, cash). The
    independent set of books ours must reconcile against."""
    import httpx

    from midas.config import settings

    if not (settings.alpaca_api_key and settings.alpaca_api_secret):
        return None
    try:
        r = httpx.get(f"{settings.alpaca_paper_base_url}/v2/account",
                      headers=_alpaca_headers(), timeout=15)
        if r.status_code == 200:
            a = r.json()
            return {"equity": float(a.get("equity") or 0),
                    "cash": float(a.get("cash") or 0)}
    except Exception as exc:
        logger.warning("alpaca account fetch failed: %s", type(exc).__name__)
    return None


def fetch_alpaca_closed_orders() -> list[dict]:
    import httpx

    from midas.config import settings

    r = httpx.get(f"{settings.alpaca_paper_base_url}/v2/orders",
                  params={"status": "closed", "limit": 500, "direction": "desc"},
                  headers=_alpaca_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def reconcile_mirrored_orders(db: Session,
                              alpaca_orders: list[dict] | None = None) -> dict:
    """Execution integrity: every ledger fill that claims an Alpaca mirror
    must match Alpaca's own record (qty exact, price within a cent).
    Decisions are scored by the market; execution is scored by the broker.
    Mismatches raise loud audit events - they mean the MACHINE is wrong,
    which matters more than any single bad trade."""
    from sqlalchemy import select

    from midas.models import Broker

    ours = db.scalars(select(Order).where(
        Order.broker == Broker.alpaca_paper,
        Order.status == OrderStatus.filled)).all()
    if not ours:
        return {"checked": 0, "matched": 0, "mismatched": 0, "unverifiable": 0}
    if alpaca_orders is None:
        try:
            alpaca_orders = fetch_alpaca_closed_orders()
        except Exception as exc:
            logger.warning("reconcile: alpaca unreachable: %s", type(exc).__name__)
            return {"checked": 0, "matched": 0, "mismatched": 0,
                    "unverifiable": len(ours), "note": "alpaca unreachable"}
    theirs = {o.get("client_order_id"): o for o in alpaca_orders}
    matched = mismatched = unverifiable = 0
    for o in ours:
        record = theirs.get(str(o.id))
        if record is None:
            unverifiable += 1  # older than the fetch window, most likely
            continue
        their_qty = float(record.get("filled_qty") or 0)
        their_price = float(record.get("filled_avg_price") or 0)
        qty_ok = abs(their_qty - o.qty) < 1e-9
        price_ok = abs(their_price - (o.filled_price or 0)) <= 0.01
        if qty_ok and price_ok:
            matched += 1
        else:
            mismatched += 1
            record_event(
                db, event_type="execution.mismatch",
                summary=(f"MIRROR MISMATCH {o.symbol}: ledger "
                         f"{o.qty:g}@{o.filled_price} vs alpaca "
                         f"{their_qty:g}@{their_price}"),
                subject_type="order", subject_id=o.id,
                payload={"ledger_qty": o.qty, "ledger_price": o.filled_price,
                         "alpaca_qty": their_qty, "alpaca_price": their_price},
            )
    return {"checked": len(ours), "matched": matched,
            "mismatched": mismatched, "unverifiable": unverifiable}


def refresh_equity(db: Session) -> float:
    """Mark open positions to market and refresh account equity."""
    account = get_account(db)
    open_pos = list(db.query(Position).filter(Position.status == PositionStatus.open))
    value = 0.0
    for p in open_pos:
        try:
            price = mark_price(db, _pseudo_order(p))
            p.last_mark_price = price
        except Exception:  # keep the last mark if the source is down
            logger.warning("mark failed for %s; using last mark", p.symbol)
            price = p.last_mark_price or p.avg_price
        multiplier = 100 if p.instrument == Instrument.option else 1
        value += price * p.qty * multiplier
    equity = account["cash"] + value
    set_account(db, cash=account["cash"], equity=equity)
    return equity


def _pseudo_order(p: Position) -> Order:
    o = Order(thesis_id=p.thesis_id, side=OrderSide.sell, instrument=p.instrument,
              symbol=p.symbol, qty=p.qty, reason="exit")
    o.position_id = p.id
    return o
