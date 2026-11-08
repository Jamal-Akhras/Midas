"""Target-weight generators. Each takes a PriceData and returns a DataFrame
of target weights indexed by rebalance day (see portfolio.simulate for the
timing convention). Nothing here looks at the future: every weight on day D
is computed from closes strictly before D."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from midas.research.data import PriceData
from midas.research.portfolio import fixed_weights, monthly_calendar, on_signal_change

ScoreFn = Callable[[PriceData, Sequence[str]], pd.DataFrame]


def fixed_mix(
    data: PriceData, weights: Mapping[str, float], *, start: str | None = None
) -> pd.DataFrame:
    """A constant allocation rebalanced monthly (60/40, All Weather, ...)."""
    sig, reb = monthly_calendar(data.index)
    return fixed_weights(data.close, weights, sig, reb, start=start)


def sma_trend(
    data: PriceData, ticker: str = "SPY", *, window: int = 200, below_weight: float = 0.0
) -> pd.DataFrame:
    """Daily trend filter: fully in `ticker` while its close is above the
    moving average, `below_weight` of it otherwise (0 = all cash, 0.5 = the
    Midas half-throttle). Trades only when the signal flips."""
    close = data.close[ticker]
    sma = close.rolling(window).mean()
    above = (close > sma).shift(1).astype(float)
    weight = below_weight + (1.0 - below_weight) * above
    frame = pd.DataFrame({ticker: weight}, index=data.index)
    frame = frame[sma.shift(1).notna()]
    return on_signal_change(frame)


def sma_monthly(data: PriceData, ticker: str = "SPY", *, window: int = 210) -> pd.DataFrame:
    """Faber's rule: at month end, hold `ticker` if it closed above its
    ~10-month average, else cash."""
    close = data.close[ticker]
    sma = close.rolling(window).mean()
    sig, reb = monthly_calendar(data.index)
    rows = {
        rd: {ticker: float(close.at[sd] > sma.at[sd])}
        for sd, rd in zip(sig, reb, strict=True)
        if not np.isnan(sma.at[sd])
    }
    return pd.DataFrame(rows).T


def _trailing_return(
    series: pd.Series, index: pd.DatetimeIndex, day: pd.Timestamp, lookback: int
) -> float:
    pos = int(index.searchsorted(day))
    if pos < lookback:
        return float("nan")
    return float(series.iloc[pos] / series.iloc[pos - lookback] - 1.0)


def dual_momentum(
    data: PriceData,
    risky: Sequence[str] = ("SPY", "EFA"),
    safe: str = "AGG",
    *,
    lookback: int = 252,
) -> pd.DataFrame:
    """Antonacci's GEM. Absolute momentum first: if the first risky asset
    beat cash over the lookback, hold whichever risky asset has the higher
    trailing return (ties to the first listed); otherwise hold the safe
    asset."""
    cash_index = (1.0 + data.cash_daily).cumprod()
    sig, reb = monthly_calendar(data.index)
    rows: dict[pd.Timestamp, dict[str, float]] = {}
    for sd, rd in zip(sig, reb, strict=True):
        scores = {a: _trailing_return(data.close[a], data.index, sd, lookback) for a in risky}
        cash_score = _trailing_return(cash_index, data.index, sd, lookback)
        if any(np.isnan(v) for v in scores.values()) or np.isnan(data.close.at[sd, safe]):
            continue
        row = dict.fromkeys([*risky, safe], 0.0)
        if scores[risky[0]] > cash_score:
            row[max(risky, key=lambda a: scores[a])] = 1.0
        else:
            row[safe] = 1.0
        rows[rd] = row
    return pd.DataFrame(rows).T


def tactical_sma(data: PriceData, assets: Sequence[str], *, window: int = 210) -> pd.DataFrame:
    """Faber's GTAA: equal slices, each held only while above its own
    moving average (the unheld slice sits in cash)."""
    smas = {a: data.close[a].rolling(window).mean() for a in assets}
    sig, reb = monthly_calendar(data.index)
    slice_w = 1.0 / len(assets)
    rows = {
        rd: {a: slice_w * float(data.close.at[sd, a] > smas[a].at[sd]) for a in assets}
        for sd, rd in zip(sig, reb, strict=True)
        if all(not np.isnan(smas[a].at[sd]) for a in assets)
    }
    return pd.DataFrame(rows).T


def inverse_vol(data: PriceData, assets: Sequence[str], *, window: int = 60) -> pd.DataFrame:
    """Naive risk parity: monthly weights proportional to 1 / trailing vol."""
    vol = data.returns[list(assets)].rolling(window).std()
    sig, reb = monthly_calendar(data.index)
    rows: dict[pd.Timestamp, dict[str, float]] = {}
    for sd, rd in zip(sig, reb, strict=True):
        v = vol.loc[sd]
        if v.isna().any():
            continue
        inv = 1.0 / v
        rows[rd] = {str(k): float(x) for k, x in (inv / inv.sum()).items()}
    return pd.DataFrame(rows).T


# ---------- cross-sectional (needs a universe; today's constituents carry
# survivorship bias, which the report labels) ----------


def momentum_12_1(data: PriceData, universe: Sequence[str]) -> pd.DataFrame:
    """Classic 12-1 momentum: return from 12 months ago to 1 month ago."""
    c = data.close[list(universe)]
    return c.shift(21) / c.shift(252) - 1.0


def realized_vol(data: PriceData, universe: Sequence[str], *, window: int = 60) -> pd.DataFrame:
    return data.returns[list(universe)].rolling(window).std()


def presence(data: PriceData, universe: Sequence[str]) -> pd.DataFrame:
    """1.0 wherever a price exists: ranks everything equally (equal weight)."""
    return data.close[list(universe)].notna().astype(float)


def cross_sectional(
    data: PriceData,
    universe: Sequence[str],
    score: ScoreFn,
    *,
    top: int = 50,
    ascending: bool = False,
    trend_filter: tuple[str, int] | None = None,
    min_names: int = 100,
) -> pd.DataFrame:
    """Monthly: rank `universe` by `score` on the signal day and hold the top
    `top` names equally. With `trend_filter=(ticker, window)` the book goes
    to cash when that ticker is below its moving average."""
    scores = score(data, universe)
    close = data.close[list(universe)]
    sig, reb = monthly_calendar(data.index)
    filt_ok: pd.Series | None = None
    if trend_filter is not None:
        t, win = trend_filter
        filt_ok = data.close[t] > data.close[t].rolling(win).mean()
    rows: dict[pd.Timestamp, dict[str, float]] = {}
    for sd, rd in zip(sig, reb, strict=True):
        s = scores.loc[sd].dropna()
        s = s[close.loc[sd, s.index].notna()]
        if len(s) < min_names:
            continue
        picks = s.sort_values(ascending=ascending).index[:top]
        w = 1.0 / top
        if filt_ok is not None and not bool(filt_ok.at[sd]):
            w = 0.0
        rows[rd] = {str(t): w for t in picks}
    return pd.DataFrame(rows).T.reindex(columns=list(universe)).fillna(0.0)


def equal_weight(data: PriceData, universe: Sequence[str]) -> pd.DataFrame:
    return cross_sectional(data, universe, presence, top=len(universe), min_names=1)
