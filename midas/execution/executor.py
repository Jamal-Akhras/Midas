"""Executor: turns approved theses into paper orders and processes exit
decisions - with the autonomy ladder and an execution-time risk re-check
enforced HERE, in code, regardless of what any agent said.

L0: everything needs operator approval (thesis AND orders).
L1: approved theses auto-execute entries on paper; exits still proposed.
L2: entries and exits auto-execute on paper within caps.
There is no L3+ path in this codebase (see config).
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from midas.audit import record_event
from midas.config import settings
from midas.execution import paper_broker
from midas.models import (
    ActorType,
    ApprovalRequest,
    ApprovalStatus,
    Instrument,
    OptionStructure,
    Order,
    OrderReason,
    OrderSide,
    OrderStatus,
    Position,
    PositionStatus,
    ThesisStatus,
    TradeThesis,
    utcnow,
)
from midas.risk import engine as risk_engine

logger = logging.getLogger(__name__)


class ExecutionRefused(Exception):
    """The execution-time risk re-check said no. Code wins."""


class AwaitingEntryZone(ExecutionRefused):
    """Spot is above the entry zone: not an error, re-checked each pass."""


class ThesisInvalidated(ExecutionRefused):
    """Spot is at or through the stop before entry: the thesis is dead and
    has been marked rejected; it is never retried."""


def execute_thesis(db: Session, thesis: TradeThesis, *, approved_by: str) -> Order:
    """Create + submit the entry order for an approved thesis.
    Risk caps are re-checked HERE against live account/portfolio state."""
    if thesis.status != ThesisStatus.approved:
        raise ExecutionRefused(f"thesis is {thesis.status.value}, not approved")

    halt = risk_engine.get_halt(db)
    if halt:
        raise ExecutionRefused(f"pipeline halted: {halt['reason']}")

    already = db.scalar(
        select(Position).where(
            Position.ticker == thesis.ticker, Position.status == PositionStatus.open
        )
    )
    if already is not None:
        raise ExecutionRefused(
            f"position already open in {thesis.ticker} - one position per "
            f"ticker; sizing up is the PM/reevaluator's call, not a second "
            f"entry (also avoids broker wash-trade rejects)"
        )

    equity = paper_broker.refresh_equity(db)
    loss_reason = risk_engine.enforce_loss_halt(db, equity)
    if loss_reason:
        raise ExecutionRefused(f"loss limit halt: {loss_reason}")

    sector = ""
    if thesis.instrument == Instrument.option:
        structure = db.scalar(select(OptionStructure).where(OptionStructure.thesis_id == thesis.id))
        if structure is None:
            raise ExecutionRefused("option thesis without a structure row")
        if thesis.entry_zone_high:
            try:
                from midas.tools.market_data import get_quote

                spot = float(get_quote(thesis.ticker).data["price"])
            except Exception as exc:
                raise ExecutionRefused(
                    f"cannot verify entry zone (quote failed: {type(exc).__name__}); will retry"
                ) from exc
            if spot > thesis.entry_zone_high:
                raise AwaitingEntryZone(
                    f"awaiting entry zone: underlying {spot} above "
                    f"{thesis.entry_zone_high} - chasing a gap day at peak "
                    f"IV is the error this gate exists to prevent; retried "
                    f"each pass"
                )
        rc = risk_engine.check_option_trade(
            db,
            account_equity=equity,
            premium_per_contract=structure.est_premium_per_contract,
            dte=structure.dte_at_entry,
        )
        if not rc.ok or rc.max_qty < 1:
            raise ExecutionRefused(f"execution-time risk re-check failed: {rc.checks}")
        qty = min(structure.contracts or rc.max_qty, rc.max_qty)
        symbol = paper_broker.option_symbol(
            structure.underlying,
            structure.expiry.isoformat(),
            structure.option_type.value,
            structure.long_strike,
            structure.short_strike,
        )
        note = (
            "debit spread; marked at net debit of both legs"
            if structure.short_strike is not None
            else ""
        )
        instrument = Instrument.option
    else:
        try:
            from midas.tools.market_data import get_quote

            quote = get_quote(thesis.ticker).data
            spot = float(quote["price"])
        except Exception as exc:
            raise ExecutionRefused(
                f"cannot verify entry zone (quote failed: {type(exc).__name__}); will retry"
            ) from exc
        sector = str(quote.get("sector") or "")
        _gate_shares_entry(db, thesis, spot)
        rc = risk_engine.check_shares_trade(
            db,
            account_equity=equity,
            entry_price=thesis.entry_zone_high,
            stop_price=thesis.stop_price,
            sector=sector,
            first_target=(float(thesis.targets[0]) if thesis.targets else None),
        )
        # the engine already floors the size at MIN_SWING_NOTIONAL_USD; a
        # fraction of a share is a valid size (FRACTIONAL_SHARES)
        if not rc.ok or rc.max_qty <= 0:
            raise ExecutionRefused(f"execution-time risk re-check failed: {rc.checks}")
        qty = rc.max_qty
        symbol = thesis.ticker
        note = ""
        instrument = Instrument.shares

    order = Order(
        thesis_id=thesis.id,
        side=OrderSide.buy,
        instrument=instrument,
        symbol=symbol,
        qty=qty,
        reason=OrderReason.entry,
        limit_price=(thesis.entry_zone_high if instrument == Instrument.shares else None),
        approved_by=approved_by,
        risk_check=rc.to_content(),
        error=note,
        status=OrderStatus.approved,
    )
    db.add(order)
    db.flush()
    record_event(
        db,
        event_type="order.submitted",
        summary=f"entry order for {thesis.ticker} ({approved_by})",
        actor_type=ActorType.system,
        run_id=thesis.run_id,
        subject_type="order",
        subject_id=order.id,
    )
    paper_broker.submit(db, order)
    if order.status == OrderStatus.filled:
        if not sector:
            try:
                from midas.tools.market_data import get_quote

                sector = str(get_quote(thesis.ticker).data.get("sector") or "")
            except Exception:
                pass
        paper_broker.apply_fill_to_position(db, order, thesis, sector=sector)
        thesis.status = ThesisStatus.executed
        _schedule_first_review(db, thesis, order)
        # a fill is a fact at the broker: it must never sit in an open
        # transaction that a later failure in the same pass could roll back
        db.commit()
    return order


def _gate_shares_entry(db: Session, thesis: TradeThesis, spot: float) -> None:
    """The live quote decides whether a shares entry may go out NOW.
    Through the stop: the thesis is already invalidated and is rejected for
    good (buying it would be a stop-out in the making). Above the zone:
    wait - the price may come back within the thesis's 5-day life."""
    if thesis.stop_price and spot <= thesis.stop_price:
        thesis.status = ThesisStatus.rejected
        record_event(
            db,
            event_type="thesis.invalidated_before_entry",
            summary=(
                f"{thesis.ticker}: spot {spot:g} is at/through the stop "
                f"{thesis.stop_price:g} before entry - thesis rejected, no order"
            ),
            run_id=thesis.run_id,
            subject_type="thesis",
            subject_id=thesis.id,
            payload={
                "spot": spot,
                "stop": thesis.stop_price,
                "entry_zone": [thesis.entry_zone_low, thesis.entry_zone_high],
            },
        )
        db.flush()
        raise ThesisInvalidated(
            f"{thesis.ticker}: invalidated before entry (spot {spot:g} <= stop "
            f"{thesis.stop_price:g}); thesis rejected"
        )
    if thesis.entry_zone_high and spot > thesis.entry_zone_high:
        raise AwaitingEntryZone(
            f"awaiting entry zone: {thesis.ticker} {spot:g} above "
            f"{thesis.entry_zone_high:g}; re-checked each pass"
        )


def propose_or_execute_exit(
    db: Session,
    position: Position,
    *,
    decision: str,
    reasons: list[str],
    artifact_id: uuid.UUID | None = None,
) -> Order | ApprovalRequest | None:
    """Route an exit/trim decision by autonomy level: L2 executes on paper;
    below L2 it becomes a blocking approval request for the operator.
    None: a trim that would over-sell or leave dust was skipped (audited)."""
    if decision in ("exit", "roll"):
        qty = position.qty
    else:
        trim = _trim_qty(db, position)
        if trim is None:
            return None
        qty = trim
    thesis = db.get(TradeThesis, position.thesis_id)
    if settings.autonomy_level >= 2:
        order = Order(
            thesis_id=position.thesis_id,
            position_id=position.id,
            side=OrderSide.sell,
            instrument=position.instrument,
            symbol=position.symbol,
            qty=qty,
            reason=OrderReason.exit if decision in ("exit", "roll") else OrderReason.trim,
            approved_by=f"investor_agent(L{settings.autonomy_level})",
            status=OrderStatus.approved,
        )
        db.add(order)
        db.flush()
        paper_broker.submit(db, order)
        if order.status == OrderStatus.filled:
            paper_broker.apply_fill_to_position(db, order, thesis)
            db.commit()  # a broker fill never waits on the rest of the pass
        return order

    req = ApprovalRequest(
        run_id=None,
        subject_type="order_proposal",
        subject_id=position.id,
        title=f"{decision.upper()} {position.symbol} ({qty:g})",
        summary="; ".join(reasons),
        context={
            "decision": decision,
            "qty": qty,
            "position_id": str(position.id),
            "artifact_id": str(artifact_id) if artifact_id else None,
        },
        blocking=True,
    )
    db.add(req)
    db.flush()
    record_event(
        db,
        event_type="approval.requested",
        summary=f"{position.symbol}: {decision} proposed, awaiting operator",
        subject_type="position",
        subject_id=position.id,
    )
    return req


def execute_approved_exit(db: Session, approval: ApprovalRequest) -> Order:
    """Operator approved an exit proposal - submit it."""
    if approval.status != ApprovalStatus.approved:
        raise ExecutionRefused("exit approval is not approved")
    position = db.get(Position, uuid.UUID(approval.context["position_id"]))
    if position is None:
        raise ExecutionRefused("exit approval refers to a position that no longer exists")
    thesis = db.get(TradeThesis, position.thesis_id)
    decision = approval.context["decision"]
    order = Order(
        thesis_id=position.thesis_id,
        position_id=position.id,
        side=OrderSide.sell,
        instrument=position.instrument,
        symbol=position.symbol,
        qty=float(approval.context["qty"]),
        reason=OrderReason.exit if decision in ("exit", "roll") else OrderReason.trim,
        approved_by="operator",
        status=OrderStatus.approved,
    )
    db.add(order)
    db.flush()
    paper_broker.submit(db, order)
    if order.status == OrderStatus.filled:
        paper_broker.apply_fill_to_position(db, order, thesis)
        db.commit()  # a broker fill never waits on the rest of the pass
    return order


def _trim_qty(db: Session, position: Position) -> float | None:
    """Half the position: three decimals for fractional shares, whole units
    otherwise. None (audited) when the trim would over-sell, close the
    position, or leave dust below MIN_SWING_NOTIONAL_USD - hold or exit are
    the honest choices then. The old max(int(qty / 2), 1) sold a whole
    share out of a 0.5-share position."""
    fractional = settings.fractional_shares and position.instrument == Instrument.shares
    qty = round(position.qty / 2, 3) if fractional else float(max(int(position.qty / 2), 1))
    qty = min(qty, position.qty)
    remainder = position.qty - qty
    multiplier = 100 if position.instrument == Instrument.option else 1
    price = position.last_mark_price or position.avg_price
    remainder_usd = remainder * price * multiplier
    floor = float(settings.min_swing_notional_usd)
    if qty <= 0 or remainder <= 0 or remainder_usd < floor:
        logger.info(
            "%s: trim skipped - would leave %g (= $%.2f, floor $%.0f)",
            position.symbol,
            remainder,
            remainder_usd,
            floor,
        )
        record_event(
            db,
            event_type="exit.trim_skipped",
            summary=(
                f"{position.symbol}: trim of {qty:g} would leave {remainder:g} "
                f"(${remainder_usd:,.2f}), below the ${floor:,.0f} floor - hold or exit instead"
            ),
            subject_type="position",
            subject_id=position.id,
            payload={
                "qty": position.qty,
                "trim_qty": qty,
                "remainder_usd": round(remainder_usd, 2),
                "floor_usd": floor,
            },
        )
        return None
    return qty


def _schedule_first_review(db: Session, thesis: TradeThesis, order: Order) -> None:
    from datetime import timedelta

    position = db.get(Position, order.position_id)
    assert position is not None, "a filled entry order always has a position"
    cadence = int((thesis.review_schedule or {}).get("cadence_days", 1))
    position.next_review_at = utcnow() + timedelta(days=cadence)
    db.flush()


# ---------- approvals plumbing (operator actions) ----------


def resolve_approval(
    db: Session, approval: ApprovalRequest, *, approve: bool, note: str = ""
) -> None:
    approval.status = ApprovalStatus.approved if approve else ApprovalStatus.denied
    approval.resolved_at = utcnow()
    approval.resolution_note = note
    record_event(
        db,
        event_type=f"approval.{approval.status.value}",
        summary=f"{approval.title}: {approval.status.value} by operator",
        actor_type=ActorType.operator,
        subject_type=approval.subject_type,
        subject_id=approval.subject_id,
    )
    if approval.subject_type == "thesis":
        thesis = db.get(TradeThesis, approval.subject_id)
        if thesis is None:
            raise ExecutionRefused("approval refers to a thesis that no longer exists")
        thesis.status = ThesisStatus.approved if approve else ThesisStatus.rejected
        if approve:
            execute_thesis(db, thesis, approved_by="operator")
    elif approval.subject_type == "order_proposal" and approve:
        execute_approved_exit(db, approval)
    elif approval.subject_type == "core_plan" and approve:
        from midas.strategy import core as core_book

        core_book.execute_plan(db, approval.subject_id, approved_by="operator")
