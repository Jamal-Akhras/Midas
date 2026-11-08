"""Performance measurement. Sharpe is excess return over the cash series
(not zero), so a strategy that merely holds T-bills scores zero."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass(frozen=True)
class Performance:
    name: str
    start: str
    end: str
    years: float
    cagr: float
    cash_cagr: float
    vol: float
    sharpe: float
    max_drawdown: float
    worst_year: float
    pct_years_beat_cash: float
    growth_of_1k: float
    note: str = ""

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def cagr(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    r = returns.dropna()
    if r.empty:
        return float("nan")
    growth = float((1.0 + r).prod())
    years = len(r) / periods_per_year
    if growth <= 0:
        return -1.0
    return float(growth ** (1.0 / years)) - 1.0


def annual_vol(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    return float(returns.dropna().std() * math.sqrt(periods_per_year))


def sharpe(
    returns: pd.Series, cash: pd.Series | None = None, periods_per_year: int = TRADING_DAYS
) -> float:
    r = returns.dropna()
    if r.empty or float(r.std()) == 0.0:
        return float("nan")
    rf = cash.reindex(r.index).fillna(0.0) if cash is not None else pd.Series(0.0, index=r.index)
    excess = r - rf
    return float(excess.mean() / r.std() * math.sqrt(periods_per_year))


def max_drawdown(returns: pd.Series) -> float:
    curve = (1.0 + returns.dropna()).cumprod()
    if curve.empty:
        return float("nan")
    return float((curve / curve.cummax() - 1.0).min())


def calendar_year_returns(returns: pd.Series) -> pd.Series:
    r = returns.dropna()
    idx = pd.DatetimeIndex(r.index)
    return (1.0 + r).groupby(idx.year).prod() - 1.0


def summarize(
    returns: pd.Series,
    name: str,
    *,
    cash: pd.Series | None = None,
    note: str = "",
    min_periods: int = 250,
    periods_per_year: int = TRADING_DAYS,
) -> Performance | None:
    """Full summary, or None when the series is too short to mean anything."""
    r = returns.dropna()
    if len(r) < min_periods:
        return None
    rf = cash.reindex(r.index).fillna(0.0) if cash is not None else pd.Series(0.0, index=r.index)
    yearly = calendar_year_returns(r)
    cash_yearly = calendar_year_returns(rf)
    years = len(r) / periods_per_year
    growth = float((1.0 + r).prod())
    return Performance(
        name=name,
        start=str(pd.Timestamp(r.index[0]).date()),
        end=str(pd.Timestamp(r.index[-1]).date()),
        years=round(years, 1),
        cagr=cagr(r, periods_per_year),
        cash_cagr=cagr(rf, periods_per_year),
        vol=annual_vol(r, periods_per_year),
        sharpe=sharpe(r, rf, periods_per_year),
        max_drawdown=max_drawdown(r),
        worst_year=float(yearly.min()),
        pct_years_beat_cash=float((yearly > cash_yearly.reindex(yearly.index)).mean()),
        growth_of_1k=1000.0 * growth,
        note=note,
    )


@dataclass(frozen=True)
class TradeStats:
    """Per-trade view for strategies that trade discretely (intraday rules,
    the swing clone)."""

    days: int
    trades: int
    win_rate: float
    avg_trade_bps: float
    avg_r: float | None = None

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def trade_stats(daily_returns: pd.Series, r_multiples: pd.Series | None = None) -> TradeStats:
    r = daily_returns.dropna()
    active = r[r != 0.0]
    return TradeStats(
        days=len(r),
        trades=int(len(active)),
        win_rate=float((active > 0).mean()) if len(active) else float("nan"),
        avg_trade_bps=float(active.mean() * 1e4) if len(active) else float("nan"),
        avg_r=(float(np.nanmean(r_multiples.to_numpy())) if r_multiples is not None else None),
    )
