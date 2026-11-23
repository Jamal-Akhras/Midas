"""The deterministic skeleton of the Midas swing committee, as a backtest.

Entry: pullback in an uptrend (close above the 50-day, 20-day above 50-day,
within 12% of the 20-day high, not extended from the 10-day), scored and
ranked, sized at `risk_pct` of equity with a fixed percentage stop, capped
by per-position and total notional limits, throttled when the regime
ticker is below its 200-day average. Exits: stop (gap-through at the open),
time stop, break-even ratchet at +1R then a trailing N-day low.

Those defaults are the rules of early November 2026 and the figures in
docs/results.md. The rules added since are opt-in, one keyword each, so
the live configuration can be replayed (midas.research.account) without
moving the published numbers:

- `require_reclaim`: the signal day must close above its 10-day mean and
  the prior close.
- `stop_atr`: the stop is structural (under the lowest low since the
  20-day high, never nearer than this many 14-day ATRs to the close).
- `min_room`: the 20-day high must be at least this many stop distances
  above the close.
- `entry_wait_days`: the entry is on strength. For this many sessions the
  order fills only at or above the prior close and inside a zone that
  runs from the signal close up `zone_atr` ATRs, and not on a day the
  index gaps down more than `max_index_gap_pct`.
- `trial_trades`: once this many trades have closed with an average
  result at or below zero, no further entries.

No LLM is involved: this measures what the committee's rules alone would
have done, with the LLM's stock selection replaced by the scored screen.
Daily bars cannot see inside a session, so two choices are stated here
rather than hidden. An order that opens under the prior close and trades
back through it is filled at the prior close and is not stopped that day
(the low came first). And the index condition reads the opening gap, not
the index at the minute of the fill."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from midas.research.data import PriceData


@dataclass(frozen=True)
class CloneStats:
    n_trades: int = 0
    win_rate: float = float("nan")
    avg_r: float = float("nan")
    avg_days: float = float("nan")
    profit_factor: float = float("nan")
    exits: dict[str, int] = field(default_factory=dict)
    avg_pct: float = float("nan")  # mean result per trade, percent of the entry
    exposure: float = 0.0  # mean share of equity invested
    max_exposure: float = 0.0
    committees: int = 0  # gate-passers a daily cycle would have sent to a committee
    trial_ended: str = ""  # the date the trial rule stopped new entries, if it did
    trade_pcts: tuple[float, ...] = ()  # every closed trade, in the order they closed

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Position:
    qty: float
    entry: float
    stop: float
    r0: float
    day_i: int
    risk_usd: float
    breakeven: bool = False
    no_exit_on: int = -1  # the entry day of a fill that came after the day's low


@dataclass(frozen=True)
class _Pending:
    stop: float
    zone_low: float
    zone_high: float
    expires: int


def structure_stop(
    high: np.ndarray, low: np.ndarray, *, close: float, atr: float, min_atr: float
) -> float:
    """The stop the chart names: under the lowest low since the highest
    high of the window, and never nearer to the close than `min_atr` ATRs.
    `high` and `low` are the window ending on the signal bar."""
    peak = int(np.nanargmax(high))
    swing_low = float(np.nanmin(low[peak:]))
    return min(swing_low, close - min_atr * atr)


def strength_fill(
    *, open_: float, high: float, low: float, prior_close: float, zone_low: float, zone_high: float
) -> float | None:
    """The price an entry on strength fills at on one daily bar, or None.
    A fill needs the price at or above the prior close (the reclaim still
    standing) and inside the zone."""
    floor = max(prior_close, zone_low)
    if floor > zone_high:
        return None  # nothing inside the zone is at or above the prior close
    if floor <= open_ <= zone_high:
        return open_
    if open_ < floor:
        return floor if high >= floor else None  # bought as it trades back through
    return zone_high if low <= zone_high else None  # gapped above, came back into the zone


def trial_failed(trade_pcts: Sequence[float], trial_trades: int) -> bool:
    """The trial rule of midas.risk.trial: read the first `trial_trades`
    closed trades and nothing after them; an average at or below zero fails."""
    if trial_trades <= 0 or len(trade_pcts) < trial_trades:
        return False
    return sum(trade_pcts[:trial_trades]) / trial_trades <= 0


def midas_clone(
    data: PriceData,
    universe: Sequence[str],
    *,
    stop_pct: float = 0.05,
    time_stop_days: int = 15,
    risk_pct: float = 1.0,
    max_positions: int = 8,
    max_new_per_day: int = 2,
    notional_cap: float = 0.15,
    total_cap: float = 0.60,
    regime: bool = True,
    throttle: float = 0.5,
    regime_ticker: str = "SPY",
    breakeven_r: float = 1.0,
    trail_days: int = 10,
    cost_bps: float = 5.0,
    start: str = "2007-01-01",
    score_min: float = 0.0,
    require_reclaim: bool = False,
    stop_atr: float = 0.0,
    min_room: float = 0.0,
    entry_wait_days: int = 0,
    zone_atr: float = 0.5,
    max_index_gap_pct: float = 0.5,
    trial_trades: int = 0,
    committee_cap: int = 0,
) -> tuple[pd.Series, CloneStats]:
    """Daily returns of the clone's equity curve plus trade statistics."""
    tickers = [t for t in universe if t in data.close.columns]
    cl, op, hi, lo, vo = (
        getattr(data, k)[tickers] for k in ("close", "open", "high", "low", "volume")
    )
    idx = data.index
    ma10, ma20, ma50 = cl.rolling(10).mean(), cl.rolling(20).mean(), cl.rolling(50).mean()
    high20 = hi.rolling(20).max()
    extension = cl / ma10 - 1.0
    pullback = (1.0 - cl / high20) * 100.0
    v5, v20 = vo.rolling(5).mean(), vo.rolling(20).mean()
    score = (
        60.0
        + (25.0 - (pullback - 6.0).abs().mul(4.0).clip(upper=25.0))
        + 10.0 * (v5 / v20 < 1.0).astype(float)
        + (5.0 - extension.abs() * 100.0).clip(lower=0.0)
    )
    valid = (
        (cl > ma50)
        & (ma20 > ma50)
        & (extension <= 0.08)
        & (extension >= -0.05)
        & (pullback <= 12.0)
        & (cl >= 5.0)
        & ((cl * vo).rolling(20).mean() >= 20e6)
    )
    if require_reclaim:
        valid = valid & (cl > ma10) & (cl > cl.shift(1))
    score = score.where(valid).clip(upper=100.0)
    regime_close = data.close[regime_ticker]
    risk_on = (regime_close > regime_close.rolling(200).mean()).to_numpy()
    index_gap = (data.open[regime_ticker] / regime_close.shift(1) - 1.0).to_numpy() * 100.0
    prior = cl.shift(1)
    true_range = pd.DataFrame(
        np.maximum.reduce(
            [(hi - lo).to_numpy(), (hi - prior).abs().to_numpy(), (lo - prior).abs().to_numpy()]
        ),
        index=idx,
        columns=tickers,
    )
    atr14 = true_range.rolling(14).mean()

    equity = 1.0
    nav = 1.0
    positions: dict[str, _Position] = {}
    pending: dict[str, _Pending] = {}
    curve = pd.Series(np.nan, index=idx)
    trades: list[dict[str, Any]] = []
    trade_pcts: list[float] = []
    exposures: list[float] = []
    committees = 0
    trial_ended = ""
    clv, opv, lov, hiv, atrv, h20v = (f.to_numpy() for f in (cl, op, lo, hi, atr14, high20))
    col = {t: j for j, t in enumerate(tickers)}
    start_pos = int(idx.searchsorted(pd.Timestamp(start)))

    def size(px_eff: float, stop: float, thr: float) -> float:
        """Shares for one entry: the risk budget, then the notional caps."""
        r0 = px_eff - stop
        qty = nav * risk_pct / 100.0 * thr / r0
        qty = min(qty, nav * notional_cap * thr / px_eff)
        deployed = sum(p.qty * p.entry for p in positions.values())
        if deployed + qty * px_eff > nav * total_cap:
            qty = max((nav * total_cap - deployed) / px_eff, 0.0)
        return qty if qty * px_eff >= nav * 0.005 else 0.0

    for i in range(start_pos, len(idx)):
        # 0) entries on strength waiting for their price, on today's bar
        if pending:
            thr = 1.0 if (not regime or risk_on[i - 1]) else throttle
            tape_ok = not (index_gap[i] < -max_index_gap_pct)
            for t in list(pending):
                order = pending[t]
                if i > order.expires:
                    del pending[t]
                    continue
                j = col[t]
                if thr <= 0 or not tape_ok or np.isnan(opv[i, j]) or np.isnan(clv[i - 1, j]):
                    continue
                fill = strength_fill(
                    open_=float(opv[i, j]),
                    high=float(hiv[i, j]),
                    low=float(lov[i, j]),
                    prior_close=float(clv[i - 1, j]),
                    zone_low=order.zone_low,
                    zone_high=order.zone_high,
                )
                if fill is None:
                    continue
                del pending[t]
                px_eff = fill * (1.0 + cost_bps / 1e4)
                if px_eff <= order.stop:
                    continue
                qty = size(px_eff, order.stop, thr)
                if qty <= 0:
                    continue
                positions[t] = _Position(
                    qty=qty,
                    entry=px_eff,
                    stop=order.stop,
                    r0=px_eff - order.stop,
                    day_i=i,
                    risk_usd=qty * (px_eff - order.stop),
                    no_exit_on=i if float(opv[i, j]) < fill else -1,
                )
        # 1) manage open positions with today's bar
        for t in list(positions):
            p = positions[t]
            j = col[t]
            o, low_, c = opv[i, j], lov[i, j], clv[i, j]
            if np.isnan(c):
                continue
            exit_px: float | None = None
            kind = ""
            if p.no_exit_on == i:
                pass  # filled after the day's low: the bar before the fill is not this trade's
            elif o <= p.stop and p.day_i != i:
                exit_px, kind = float(o), "stop_gap"
            elif low_ <= p.stop:
                exit_px, kind = p.stop, "stop"
            elif time_stop_days > 0 and i - p.day_i >= time_stop_days:
                exit_px, kind = float(c), "time"
            if exit_px is not None:
                exit_px *= 1.0 - cost_bps / 1e4
                pnl = (exit_px - p.entry) * p.qty
                equity += pnl
                trades.append(
                    {
                        "ticker": t,
                        "kind": kind,
                        "days": i - p.day_i,
                        "pnl": pnl,
                        "r": pnl / p.risk_usd if p.risk_usd else float("nan"),
                    }
                )
                trade_pcts.append((exit_px / p.entry - 1.0) * 100.0)
                del positions[t]
                if not trial_ended and trial_failed(trade_pcts, trial_trades):
                    trial_ended = str(idx[i].date())
                    pending.clear()
                continue
            if breakeven_r > 0 and not p.breakeven and c >= p.entry + breakeven_r * p.r0:
                p.stop = max(p.stop, p.entry)
                p.breakeven = True
            if p.breakeven and trail_days > 0:
                trail = float(np.nanmin(lov[i - trail_days + 1 : i + 1, j]))
                if trail < c:
                    p.stop = max(p.stop, trail)
        # mark to market
        market_value = sum(
            p.qty * (clv[i, col[t]] if not np.isnan(clv[i, col[t]]) else p.entry)
            for t, p in positions.items()
        )
        cash_value = equity - sum(p.qty * p.entry for p in positions.values())
        nav = cash_value + market_value
        curve.iloc[i] = nav
        exposures.append(market_value / nav if nav else 0.0)
        # 2) new entries from today's close
        if i + 1 >= len(idx) or trial_ended:
            if trial_ended:
                continue
            break
        thr = 1.0 if (not regime or risk_on[i]) else throttle
        if thr <= 0:
            continue
        ranked = score.iloc[i].dropna()
        ranked = ranked[ranked >= score_min].sort_values(ascending=False)
        n_new = 0
        passed = 0
        for t in ranked.index:
            full = n_new >= max_new_per_day or len(positions) + len(pending) >= max_positions
            if full and passed >= committee_cap:
                break
            j = col[t]
            close = float(clv[i, j])
            stop: float | None = None
            if stop_atr > 0:
                atr = float(atrv[i, j])
                if np.isnan(atr) or atr <= 0 or i < 19:
                    continue
                stop = structure_stop(
                    hiv[i - 19 : i + 1, j],
                    lov[i - 19 : i + 1, j],
                    close=close,
                    atr=atr,
                    min_atr=stop_atr,
                )
                overhead = float(h20v[i, j])
                if stop >= close:
                    continue
                if (
                    min_room > 0
                    and overhead > close
                    and (overhead - close) / (close - stop) < (min_room)
                ):
                    continue  # the retest does not pay for the stop
            passed += 1
            if passed <= committee_cap:
                committees += 1
            if full or t in positions or t in pending:
                continue
            if entry_wait_days > 0:
                atr = float(atrv[i, j])
                if np.isnan(atr) or atr <= 0:
                    continue
                pending[str(t)] = _Pending(
                    stop=stop if stop is not None else close * (1.0 - stop_pct),
                    zone_low=close,
                    zone_high=close + zone_atr * atr,
                    expires=i + entry_wait_days,
                )
                n_new += 1
                continue
            px = opv[i + 1, j]
            if np.isnan(px) or px <= 0:
                continue
            px_eff = float(px) * (1.0 + cost_bps / 1e4)
            if stop is None:
                stop = float(px) * (1.0 - stop_pct)
            if px_eff <= stop:
                continue
            qty = size(px_eff, stop, thr)
            if qty <= 0:
                continue
            positions[str(t)] = _Position(
                qty=qty,
                entry=px_eff,
                stop=stop,
                r0=px_eff - stop,
                day_i=i + 1,
                risk_usd=qty * (px_eff - stop),
            )
            n_new += 1

    returns = curve.dropna().pct_change().dropna()
    returns.name = "midas_clone"
    exposure = float(np.mean(exposures)) if exposures else 0.0
    max_exposure = float(np.max(exposures)) if exposures else 0.0
    if not trades:
        return returns, CloneStats(
            exposure=exposure, max_exposure=max_exposure, committees=committees
        )
    tr = pd.DataFrame(trades)
    wins = tr.loc[tr["pnl"] > 0, "pnl"].sum()
    losses = -tr.loc[tr["pnl"] < 0, "pnl"].sum()
    stats = CloneStats(
        n_trades=len(tr),
        win_rate=float((tr["pnl"] > 0).mean()),
        avg_r=float(tr["r"].mean()),
        avg_days=float(tr["days"].mean()),
        profit_factor=float(wins / max(losses, 1e-9)),
        exits={str(k): int(v) for k, v in tr["kind"].value_counts().items()},
        avg_pct=float(np.mean(trade_pcts)),
        exposure=exposure,
        max_exposure=max_exposure,
        committees=committees,
        trial_ended=trial_ended,
        trade_pcts=tuple(trade_pcts),
    )
    return returns, stats
