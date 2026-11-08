"""Portfolio simulation: target weights on rebalance days, weights drift
with returns in between, and every unit of turnover pays `cost_bps`.

A row of `target_weights` dated D means "hold these weights from D's open",
i.e. the signal was computed on the close before D. Columns are asset
tickers; whatever is not allocated sits in cash and earns `cash_daily`."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd


def simulate(
    target_weights: pd.DataFrame,
    returns: pd.DataFrame,
    cash_daily: pd.Series,
    *,
    cost_bps: float = 5.0,
) -> pd.Series:
    """Daily portfolio returns from the first rebalance onwards.

    Weights above a total of 1 are scaled down (no leverage); negative
    weights are floored at zero (long only). Turnover cost is charged on
    the day of the rebalance, before that day's return."""
    assets = [str(c) for c in target_weights.columns if c != "CASH"]
    idx = returns.index
    asset_returns = returns[assets].reindex(idx).fillna(0.0).to_numpy()
    cash_returns = cash_daily.reindex(idx).fillna(0.0).to_numpy()
    targets = target_weights[assets].reindex(idx).to_numpy(dtype=float)
    has_target = ~np.isnan(targets).all(axis=1)

    n = len(assets)
    weights = np.zeros(n)
    cash = 1.0
    started = False
    out = np.full(len(idx), np.nan)
    for i in range(len(idx)):
        if has_target[i]:
            target = np.clip(np.nan_to_num(targets[i]), 0.0, None)
            if target.sum() > 1.0:
                target = target / target.sum()
            current = weights if started else np.zeros(n)
            turnover = float(np.abs(target - current).sum())
            cost = turnover * cost_bps / 1e4
            weights = target * (1.0 - cost)
            cash = (1.0 - float(target.sum())) * (1.0 - cost)
            started = True
        if not started:
            continue
        growth = float((weights * (1.0 + asset_returns[i])).sum()) + cash * (1.0 + cash_returns[i])
        out[i] = growth - 1.0
        weights = weights * (1.0 + asset_returns[i]) / growth
        cash = cash * (1.0 + cash_returns[i]) / growth
    return pd.Series(out, index=idx, name="portfolio").dropna()


def monthly_calendar(index: pd.DatetimeIndex) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    """(signal_days, rebalance_days): each month's last trading day and the
    first trading day after it. Signals read the month-end close and trade
    the next open."""
    month_ends = pd.Series(index, index=index).groupby(index.to_period("M")).tail(1).index
    positions = index.searchsorted(month_ends) + 1
    positions = positions[positions < len(index)]
    rebalance_days = index[positions]
    signal_days = index[index.searchsorted(rebalance_days) - 1]
    return pd.DatetimeIndex(signal_days), pd.DatetimeIndex(rebalance_days)


def fixed_weights(
    close: pd.DataFrame,
    weights: Mapping[str, float],
    signal_days: pd.DatetimeIndex,
    rebalance_days: pd.DatetimeIndex,
    *,
    start: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """A constant mix, rebalanced on each rebalance day once every asset has
    a price on the signal day."""
    rows: dict[pd.Timestamp, dict[str, float]] = {}
    begin = pd.Timestamp(start) if start is not None else None
    for sd, rd in zip(signal_days, rebalance_days, strict=True):
        if begin is not None and rd < begin:
            continue
        if all(not np.isnan(close.at[sd, a]) for a in weights):
            rows[rd] = dict(weights)
    return pd.DataFrame(rows).T


def on_signal_change(weights: pd.DataFrame) -> pd.DataFrame:
    """Keep only the rows where the target changed: a daily signal becomes a
    rebalance only when it flips, so no turnover is charged for standing
    still."""
    changed = weights.ne(weights.shift(1)).any(axis=1)
    return weights[changed]
