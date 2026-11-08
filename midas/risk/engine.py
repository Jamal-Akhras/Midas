"""Risk/Sizing engine: DETERMINISTIC CODE, not a prompt.

Position size from account equity, per-trade risk cap, max total options
premium at risk, daily/weekly loss limits that HALT the pipeline, max open
positions, sector concentration. Checked at recommendation time AND again
at execution time.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from midas.config import settings
from midas.models import (
    Instrument,
    Position,
    PositionStatus,
    SystemState,
    TradeOutcome,
    aware,
    utcnow,
)

HALT_KEY = "pipeline_halt"


@dataclass
class RiskCheck:
    ok: bool
    checks: dict[str, dict] = field(default_factory=dict)  # name -> {ok, value, limit, note}
    max_qty: float = 0.0  # shares or contracts allowed
    max_premium_usd: float = 0.0  # options only

    def to_content(self) -> dict:
        return {
            "ok": self.ok,
            "checks": self.checks,
            "max_qty": self.max_qty,
            "max_premium_usd": round(self.max_premium_usd, 2),
        }


def _check(checks: dict, name: str, ok: bool, value, limit, note: str = "") -> bool:
    checks[name] = {"ok": ok, "value": value, "limit": limit, "note": note}
    return ok


# ---------- halts ----------


def get_halt(db: Session) -> dict | None:
    row = db.get(SystemState, HALT_KEY)
    if row and row.value.get("active"):
        return row.value
    return None


def set_halt(db: Session, reason: str, until_iso: str = "") -> None:
    row = db.get(SystemState, HALT_KEY)
    value = {"active": True, "reason": reason, "until": until_iso, "set_at": utcnow().isoformat()}
    if row is None:
        db.add(SystemState(key=HALT_KEY, value=value))
    else:
        row.value = value
    db.flush()


def clear_halt(db: Session) -> None:
    row = db.get(SystemState, HALT_KEY)
    if row is not None:
        row.value = {"active": False}
        db.flush()


EQUITY_ANCHOR_KEY = "equity_anchors"


def update_equity_anchors(db: Session, equity: float) -> dict:
    """Immutable-for-the-period equity snapshots: first marked equity of
    the day and of the week. Loss halts measure marked drawdown (realized
    AND unrealized) against these."""
    now = utcnow()
    day_key = now.date().isoformat()
    week_key = (now.date() - timedelta(days=now.weekday())).isoformat()
    row = db.get(SystemState, EQUITY_ANCHOR_KEY)
    value = dict(row.value) if row else {}
    # anchor to the PRIOR session's last marked equity, not this pass's:
    # an overnight gap-down must count as today's loss, not be baked into
    # today's baseline
    carry = value.get("last_equity", equity)
    if value.get("day") != day_key:
        value.update(day=day_key, day_equity=carry)
    if value.get("week") != week_key:
        value.update(week=week_key, week_equity=carry)
    value["last_equity"] = equity
    if row is None:
        db.add(SystemState(key=EQUITY_ANCHOR_KEY, value=value))
    else:
        row.value = value
    db.flush()
    return value


def loss_limits_breached(db: Session, account_equity: float) -> str | None:
    """MARKED drawdown (realized + unrealized) vs the day/week equity
    anchors, plus the realized floor from closed-trade outcomes. Returns a
    reason string when breached."""
    anchors = (db.get(SystemState, EQUITY_ANCHOR_KEY) or SystemState(value={})).value
    if account_equity > 0:
        day_eq = anchors.get("day_equity")
        if (
            day_eq
            and settings.daily_loss_limit_pct > 0
            and (account_equity - day_eq) / day_eq * 100 <= -settings.daily_loss_limit_pct
        ):
            return (
                f"daily drawdown limit hit: marked equity {account_equity:.0f}"
                f" vs day anchor {day_eq:.0f}"
            )
        week_eq = anchors.get("week_equity")
        if (
            week_eq
            and settings.weekly_loss_limit_pct > 0
            and (account_equity - week_eq) / week_eq * 100 <= -settings.weekly_loss_limit_pct
        ):
            return (
                f"weekly drawdown limit hit: marked equity {account_equity:.0f}"
                f" vs week anchor {week_eq:.0f}"
            )
    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    outcomes = list(db.scalars(select(TradeOutcome)))
    day_pnl = sum(o.pnl_usd for o in outcomes if aware(o.created_at) >= day_start)
    week_pnl = sum(o.pnl_usd for o in outcomes if aware(o.created_at) >= week_start)
    if account_equity <= 0:
        return "account equity is zero or unknown"
    if (
        settings.daily_loss_limit_pct > 0
        and day_pnl < 0
        and abs(day_pnl) / account_equity * 100 >= settings.daily_loss_limit_pct
    ):
        return (
            f"daily loss limit hit: {day_pnl:.2f} USD is "
            f">= {settings.daily_loss_limit_pct}% of equity"
        )
    if (
        settings.weekly_loss_limit_pct > 0
        and week_pnl < 0
        and abs(week_pnl) / account_equity * 100 >= settings.weekly_loss_limit_pct
    ):
        return (
            f"weekly loss limit hit: {week_pnl:.2f} USD is "
            f">= {settings.weekly_loss_limit_pct}% of equity"
        )
    return None


def enforce_loss_halt(db: Session, account_equity: float) -> str | None:
    """Check loss limits; set the halt flag when breached. The halt stops
    the WHOLE pipeline until the operator clears it manually."""
    reason = loss_limits_breached(db, account_equity)
    if reason:
        set_halt(db, reason)
    return reason


# ---------- portfolio state ----------


def open_positions(db: Session) -> list[Position]:
    return list(db.scalars(select(Position).where(Position.status == PositionStatus.open)))


def open_notional_usd(db: Session) -> float:
    """Total capital tied up in open positions (shares cost + option
    premium paid)."""
    return sum(
        p.qty * p.avg_price * (100 if p.instrument == Instrument.option else 1)
        for p in open_positions(db)
    )


def open_premium_usd(db: Session) -> float:
    return sum(
        p.qty * p.avg_price * 100 for p in open_positions(db) if p.instrument == Instrument.option
    )


def regime_throttle(db: Session) -> float:
    """Risk multiplier from today's market regime. Below the 200-day the
    committee may still trade - with budgets scaled by REGIME_THROTTLE.
    The narrative ('it is just a dip') never argues with this number."""
    if not settings.regime_filter_enabled:
        return 1.0
    row = db.get(SystemState, "market_regime")
    if row is None or row.value.get("risk_on", True):
        return 1.0
    return max(0.0, min(float(settings.regime_throttle), 1.0))


# ---------- sizing ----------


def check_shares_trade(
    db: Session,
    *,
    account_equity: float,
    entry_price: float,
    stop_price: float,
    sector: str = "",
) -> RiskCheck:
    """Size a stock position: risk (entry-stop) per share against the
    per-trade risk cap; then portfolio-level caps."""
    checks: dict[str, dict] = {}
    ok = True

    halt = get_halt(db)
    ok &= _check(
        checks,
        "pipeline_not_halted",
        halt is None,
        halt["reason"] if halt else "none",
        "no active halt",
    )
    risk_per_share = entry_price - stop_price
    ok &= _check(
        checks,
        "stop_below_entry",
        risk_per_share > 0,
        round(risk_per_share, 4),
        "> 0",
        "long-only book: stop must be below entry",
    )
    ok &= _check(checks, "positive_equity", account_equity > 0, account_equity, "> 0")
    ok = bool(ok) and ok  # normalize
    throttle = regime_throttle(db)
    if throttle <= 0:
        ok = _check(
            checks, "regime", False, "risk-off (throttle 0)", "SPY above 200d, or throttle > 0"
        )
    elif throttle < 1:
        _check(checks, "regime_throttle", True, f"x{throttle}", "risk-off: budgets scaled down")
    max_qty = 0.0
    if ok and risk_per_share > 0:
        risk_budget = account_equity * settings.per_trade_risk_pct / 100 * throttle
        max_qty = (
            round(risk_budget / risk_per_share, 3)
            if settings.fractional_shares
            else int(risk_budget / risk_per_share)
        )
        _check(
            checks,
            "per_trade_risk",
            True,
            f"{settings.per_trade_risk_pct}% -> {max_qty} shares",
            f"{settings.per_trade_risk_pct}% of equity",
        )
        # notional cap: a tight stop must not concentrate the whole account
        # into one name (gap risk ignores stops)
        raw_cap = (
            account_equity
            * settings.max_notional_pct_per_trade
            / 100
            * throttle
            / max(entry_price, 0.01)
        )
        notional_cap_qty = round(raw_cap, 3) if settings.fractional_shares else int(raw_cap)
        if notional_cap_qty < max_qty:
            max_qty = notional_cap_qty
            _check(
                checks,
                "notional_cap",
                True,
                f"capped to {max_qty} shares",
                f"<= {settings.max_notional_pct_per_trade}% of equity notional",
            )
        if max_qty <= 0:
            ok = _check(
                checks, "size_nonzero", False, 0, ">= 1 share", "stop too far for the risk budget"
            )
    ok &= _portfolio_checks(
        db, checks, sector, account_equity=account_equity, new_notional=max_qty * entry_price
    )
    return RiskCheck(ok=bool(ok), checks=checks, max_qty=max_qty)


def check_option_trade(
    db: Session,
    *,
    account_equity: float,
    premium_per_contract: float,
    dte: int,
    sector: str = "",
) -> RiskCheck:
    """Size an options position. Defined-risk: max loss = premium, so the
    premium itself is the risk and must fit ALL premium caps."""
    checks: dict[str, dict] = {}
    ok = True

    halt = get_halt(db)
    ok &= _check(
        checks,
        "pipeline_not_halted",
        halt is None,
        halt["reason"] if halt else "none",
        "no active halt",
    )
    ok &= _check(
        checks,
        "min_dte",
        dte >= settings.min_dte_at_entry,
        dte,
        f">= {settings.min_dte_at_entry}",
        "swing options only; no short-dated gamma",
    )
    ok &= _check(checks, "positive_equity", account_equity > 0, account_equity, "> 0")
    ok &= _check(checks, "premium_positive", premium_per_contract > 0, premium_per_contract, "> 0")

    max_contracts = 0
    max_premium = 0.0
    throttle = regime_throttle(db)
    if throttle <= 0:
        ok = _check(
            checks, "regime", False, "risk-off (throttle 0)", "SPY above 200d, or throttle > 0"
        )
    elif throttle < 1:
        _check(checks, "regime_throttle", True, f"x{throttle}", "risk-off: budgets scaled down")
    if ok:
        per_trade_cap = (
            account_equity
            * min(settings.per_trade_risk_pct, settings.max_premium_pct_per_trade)
            / 100
            * throttle
        )
        total_cap = account_equity * settings.max_total_premium_pct / 100 * throttle
        already_open = open_premium_usd(db)
        room = max(total_cap - already_open, 0.0)
        max_premium = min(per_trade_cap, room)
        cost_per_contract = premium_per_contract * 100
        max_contracts = int(max_premium / cost_per_contract)
        _check(
            checks,
            "premium_caps",
            max_contracts >= 1,
            f"cap ${max_premium:.0f} / contract ${cost_per_contract:.0f} "
            f"-> {max_contracts} contract(s)",
            f"min({settings.per_trade_risk_pct}%, {settings.max_premium_pct_per_trade}%) "
            f"per trade; {settings.max_total_premium_pct}% total premium",
        )
        if max_contracts < 1:
            ok = False
    ok &= _portfolio_checks(
        db,
        checks,
        sector,
        account_equity=account_equity,
        new_notional=max_contracts * premium_per_contract * 100,
    )
    return RiskCheck(ok=bool(ok), checks=checks, max_qty=max_contracts, max_premium_usd=max_premium)


def _portfolio_checks(
    db: Session, checks: dict, sector: str, account_equity: float = 0.0, new_notional: float = 0.0
) -> bool:
    ok = True
    positions = open_positions(db)
    ok &= _check(
        checks,
        "max_open_positions",
        len(positions) < settings.max_open_positions,
        len(positions),
        f"< {settings.max_open_positions}",
    )
    if account_equity > 0 and new_notional > 0:
        deployed = open_notional_usd(db)
        budget = account_equity * settings.max_total_notional_pct / 100
        ok &= _check(
            checks,
            "total_exposure",
            deployed + new_notional <= budget,
            f"${deployed:,.0f} open + ${new_notional:,.0f} new",
            f"<= {settings.max_total_notional_pct}% of equity (${budget:,.0f})",
        )
    # sector fail-closed: unknown classification is a bucket with the same
    # caps, never a bypass
    label = sector or "UNKNOWN"
    in_sector = [p for p in positions if (p.sector or "UNKNOWN") == label]
    ok &= _check(
        checks,
        "sector_concentration",
        len(in_sector) < settings.max_positions_per_sector,
        f"{len(in_sector)} in {label}",
        f"< {settings.max_positions_per_sector} per sector",
    )
    if account_equity > 0 and new_notional > 0:
        sec_notional = sum(
            p.qty * p.avg_price * (100 if p.instrument == Instrument.option else 1)
            for p in in_sector
        )
        sec_budget = account_equity * settings.max_sector_notional_pct / 100
        ok &= _check(
            checks,
            "sector_notional",
            sec_notional + new_notional <= sec_budget,
            f"${sec_notional:,.0f} in {label} + ${new_notional:,.0f} new",
            f"<= {settings.max_sector_notional_pct}% of equity",
        )
    return bool(ok)
