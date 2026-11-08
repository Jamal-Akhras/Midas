"""The deterministic skeleton of the Midas swing committee, as a backtest.

Entry: pullback in an uptrend (close above the 50-day, 20-day above 50-day,
within 12% of the 20-day high, not extended from the 10-day), scored and
ranked, sized at `risk_pct` of equity with a fixed percentage stop, capped
by per-position and total notional limits, throttled when the regime
ticker is below its 200-day average. Exits: stop (gap-through at the open),
time stop, break-even ratchet at +1R then a trailing N-day low.

No LLM is involved: this measures what the committee's rules alone would
have done, with the LLM's stock selection replaced by the scored screen."""

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
    score = score.where(valid).clip(upper=100.0)
    regime_close = data.close[regime_ticker]
    risk_on = (regime_close > regime_close.rolling(200).mean()).to_numpy()

    equity = 1.0
    positions: dict[str, _Position] = {}
    curve = pd.Series(np.nan, index=idx)
    trades: list[dict[str, Any]] = []
    clv, opv, lov = (f.to_numpy() for f in (cl, op, lo))
    col = {t: j for j, t in enumerate(tickers)}
    start_pos = int(idx.searchsorted(pd.Timestamp(start)))

    for i in range(start_pos, len(idx)):
        # 1) manage open positions with today's bar
        for t in list(positions):
            p = positions[t]
            j = col[t]
            o, low_, c = opv[i, j], lov[i, j], clv[i, j]
            if np.isnan(c):
                continue
            exit_px: float | None = None
            kind = ""
            if o <= p.stop:
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
                del positions[t]
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
        # 2) new entries at tomorrow's open from today's close
        if i + 1 >= len(idx):
            break
        thr = 1.0 if (not regime or risk_on[i]) else throttle
        if thr <= 0:
            continue
        ranked = score.iloc[i].dropna()
        ranked = ranked[ranked >= score_min].sort_values(ascending=False)
        n_new = 0
        deployed = sum(p.qty * p.entry for p in positions.values())
        for t in ranked.index:
            if n_new >= max_new_per_day or len(positions) >= max_positions:
                break
            if t in positions:
                continue
            px = opv[i + 1, col[t]]
            if np.isnan(px) or px <= 0:
                continue
            px_eff = float(px) * (1.0 + cost_bps / 1e4)
            stop = float(px) * (1.0 - stop_pct)
            r0 = px_eff - stop
            risk_usd = nav * risk_pct / 100.0 * thr
            qty = risk_usd / r0
            qty = min(qty, nav * notional_cap * thr / px_eff)
            if deployed + qty * px_eff > nav * total_cap:
                qty = max((nav * total_cap - deployed) / px_eff, 0.0)
            if qty * px_eff < nav * 0.005:
                continue
            positions[str(t)] = _Position(
                qty=qty, entry=px_eff, stop=stop, r0=r0, day_i=i + 1, risk_usd=qty * r0
            )
            deployed += qty * px_eff
            n_new += 1

    returns = curve.dropna().pct_change().dropna()
    returns.name = "midas_clone"
    if not trades:
        return returns, CloneStats()
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
    )
    return returns, stats
