"""Event studies on daily bars: is a signal day followed by a better return?

A rule the live record suggests ("names the gate dropped did better",
"every entry came as the index rolled over") is a hypothesis from a
handful of sessions. Here it is put to every signal day in the data set:
the signal is read on a completed bar, the entry is the next open, the
exit is the close `horizon` sessions later, and the benchmark is held over
the same open-to-close window. Signals cluster by day, so the signals of
each day are averaged first and the t-statistic is Newey-West over those
daily means with `horizon` lags (the windows overlap).

Two things keep the tables honest. The universe is the index list of
today, so every row is lifted by survivorship; a row is read against the
baseline row beside it, never against zero. And a signal that looks good
is put next to its mirror (a down gap beside the up gap): if the mirror
drifts the same way, the drift belongs to the data set, not to the setup.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from midas.research.data import PriceData

COST_PCT = 0.20  # round trip, 10 bp a side: the stock cost docs/results.md charges
MIN_DOLLAR_VOLUME = 2e7  # 20-day average; below this a fill at the open is not a given
PULLBACK_MIN_PCT, PULLBACK_MAX_PCT = 3.0, 12.0  # the orderly pullback of the deep scan
EXTENSION_MIN, EXTENSION_MAX = -0.05, 0.08  # vs the 10-day mean: breaking down / chasing
GAP_MIN_PCT, GAP_MIN_VOLUME_RATIO = 3.0, 1.5  # the gap of midas.pipeline.committee

Spec = tuple[str, pd.DataFrame, int, str | None, str | None]  # name, mask, horizon, start, end


@dataclass(frozen=True)
class EventRow:
    name: str
    horizon: int
    signals: int
    days: int
    raw_net: float  # mean of daily means, % per trade after costs
    excess: float  # same, minus the benchmark over the same window (no cost)
    t_excess: float
    win_net: float  # % of signals above zero after costs

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def newey_west_t(values: Sequence[float] | np.ndarray | pd.Series, lags: int) -> float:
    """t-statistic of the mean with a Bartlett-weighted long-run variance.
    `lags=0` is the ordinary t-statistic (population variance)."""
    x = np.asarray(values, dtype=float)
    n = len(x)
    if n < lags + 5:
        return float("nan")
    d = x - x.mean()
    var = float(d @ d) / n
    for k in range(1, lags + 1):
        var += 2.0 * (1.0 - k / (lags + 1.0)) * float(d[k:] @ d[:-k]) / n
    if var <= 0:
        return float("nan")
    return float(x.mean() / np.sqrt(var / n))


def forward_returns(
    data: PriceData, universe: Sequence[str], horizon: int, *, benchmark: str = "SPY"
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(raw, excess) in percent for a signal on the close of each row: buy
    the next open, sell the close `horizon` sessions after that."""
    cols = list(universe)
    raw = (data.close[cols].shift(-(1 + horizon)) / data.open[cols].shift(-1) - 1.0) * 100.0
    bench = (
        data.close[benchmark].shift(-(1 + horizon)) / data.open[benchmark].shift(-1) - 1.0
    ) * 100.0
    return raw, raw.sub(bench, axis=0)


def universe_excess(raw: pd.DataFrame, liquid_names: pd.DataFrame) -> pd.DataFrame:
    """Each return minus the mean of the liquid names on the same day. The
    same-day universe is a tighter benchmark than the index for a stock
    picked from that universe: it carries the same survivorship lift and
    the same day, so what is left is the pick."""
    return raw.sub(raw.where(liquid_names).mean(axis=1), axis=0)


def event_row(
    name: str,
    mask: pd.DataFrame,
    raw: pd.DataFrame,
    excess: pd.DataFrame,
    horizon: int,
    *,
    cost_pct: float = COST_PCT,
    start: str | None = None,
    end: str | None = None,
) -> EventRow | None:
    """One line of a study: every True in `mask` is a signal. None when the
    window holds no signal with a known outcome."""
    m = mask
    if start is not None:
        m = m[m.index >= pd.Timestamp(start)]
    if end is not None:
        m = m[m.index < pd.Timestamp(end)]
    r = raw.loc[m.index].where(m)
    e = excess.loc[m.index].where(m)
    signals = int(r.notna().sum().sum())
    if signals == 0:
        return None
    daily_raw = r.mean(axis=1).dropna()
    daily_excess = e.mean(axis=1).dropna()
    outcomes = r.to_numpy(dtype=float)
    outcomes = outcomes[~np.isnan(outcomes)]
    return EventRow(
        name=name,
        horizon=horizon,
        signals=signals,
        days=len(daily_excess),
        raw_net=round(float(daily_raw.mean()) - cost_pct, 2),
        excess=round(float(daily_excess.mean()), 2),
        t_excess=round(newey_west_t(daily_excess, horizon), 1),
        win_net=round(float((outcomes - cost_pct > 0).mean()) * 100.0, 1),
    )


# ---------- signals ----------


def liquid(data: PriceData, universe: Sequence[str]) -> pd.DataFrame:
    cols = list(universe)
    return (data.close[cols] * data.volume[cols]).rolling(20).mean() > MIN_DOLLAR_VOLUME


def uptrend(close: pd.DataFrame) -> pd.DataFrame:
    """Close above the 50-day and the 20-day above the 50-day."""
    ma50 = close.rolling(50).mean()
    return (close > ma50) & (close.rolling(20).mean() > ma50)


def pullback_setup(data: PriceData, universe: Sequence[str]) -> pd.DataFrame:
    """The Midas pullback-in-uptrend, vectorised: an uptrend, 3-12% off the
    20-day high, neither chasing nor breaking down against the 10-day."""
    cols = list(universe)
    close = data.close[cols]
    extension = close / close.rolling(10).mean() - 1.0
    off_high = (1.0 - close / data.high[cols].rolling(20).max()) * 100.0
    return (
        uptrend(close)
        & (extension >= EXTENSION_MIN)
        & (extension <= EXTENSION_MAX)
        & (off_high >= PULLBACK_MIN_PCT)
        & (off_high <= PULLBACK_MAX_PCT)
    )


def reclaim(close: pd.DataFrame) -> pd.DataFrame:
    """The entry trigger: a close above the 10-day mean and the prior close."""
    return (close > close.rolling(10).mean()) & (close > close.shift(1))


def rsi(close: pd.DataFrame, period: int = 2) -> pd.DataFrame:
    """Wilder RSI: 100 after an unbroken rise, 0 after an unbroken fall."""
    delta = close.diff()
    up = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    down = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False).mean()
    total = up + down
    return (100.0 * up / total.where(total > 0)).where(delta.notna())


def volume_gap(data: PriceData, universe: Sequence[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(the gap in percent from the prior close to the open, whether the
    volume of the day was at least GAP_MIN_VOLUME_RATIO times the average
    of the 20 days before it)."""
    cols = list(universe)
    gap = (data.open[cols] / data.close[cols].shift(1) - 1.0) * 100.0
    ratio = data.volume[cols] / data.volume[cols].rolling(20).mean().shift(1)
    return gap, ratio >= GAP_MIN_VOLUME_RATIO


def _on(series: pd.Series, columns: Sequence[str]) -> pd.DataFrame:
    """A date-only condition broadcast across the universe."""
    values = np.repeat(series.to_numpy(dtype=bool)[:, None], len(columns), axis=1)
    return pd.DataFrame(values, index=series.index, columns=list(columns))


# ---------- the studies docs/results.md reports ----------


def entry_rule_studies(
    data: PriceData, universe: Sequence[str], *, benchmark: str = "SPY", recent: str = "2020-01-01"
) -> dict[str, pd.DataFrame]:
    """The four questions the November 2026 live record raised, each as a
    table of EventRows: the reclaim gate, the index at entry, buying the dip
    instead of the reclaim, and the gap-and-hold beside its mirrors."""
    cols = list(universe)
    close, open_ = data.close[cols], data.open[cols]
    ok = liquid(data, cols)
    setup = pullback_setup(data, cols) & ok
    reclaimed = reclaim(close)
    fwd = {h: forward_returns(data, cols, h, benchmark=benchmark) for h in (5, 10, 20)}
    era = recent[:4]

    def rows(specs: Sequence[Spec]) -> pd.DataFrame:
        out = [
            event_row(name, mask, fwd[h][0], fwd[h][1], h, start=start, end=end)
            for name, mask, h, start, end in specs
        ]
        return pd.DataFrame([r.as_row() for r in out if r is not None])

    gate = [
        ("uptrend, any day (baseline)", uptrend(close) & ok),
        ("pullback setup, reclaimed (the gate passes)", setup & reclaimed),
        ("pullback setup, not reclaimed (the gate drops)", setup & ~reclaimed),
    ]
    bench = data.close[benchmark]
    above10 = _on(bench > bench.rolling(10).mean(), cols)
    above200 = _on(bench > bench.rolling(200).mean(), cols)
    known200 = _on(bench.rolling(200).mean().notna(), cols)
    entry = setup & reclaimed
    index = [
        (f"reclaimed setup, {benchmark} above its 10-day mean", entry & above10),
        (f"reclaimed setup, {benchmark} at or below its 10-day mean", entry & ~above10),
        (f"reclaimed setup, {benchmark} above its 200-day", entry & above200),
        (f"reclaimed setup, {benchmark} below its 200-day", entry & ~above200 & known200),
    ]
    oversold = (close > close.rolling(200).mean()) & (rsi(close) < 10.0) & ok
    dip = "above the 200-day, RSI(2) under 10"
    gap, heavy = volume_gap(data, cols)
    up = (gap >= GAP_MIN_PCT) & heavy & ok
    held = up & (close >= open_) & (close > close.rolling(50).mean())
    gaps = [
        ("up gap on volume that held (closed above its open and the 50-day)", held),
        ("up gap on volume that failed (closed below its open)", up & (close < open_)),
        ("down gap on volume (the mirror)", (gap <= -GAP_MIN_PCT) & heavy & ok),
    ]
    return {
        "reclaim": rows(
            [(n, m, 10, None, None) for n, m in gate]
            + [(f"{n}, since {era}", m, 10, recent, None) for n, m in gate]
        ),
        "index": rows([(n, m, 10, None, None) for n, m in index]),
        "dip": rows(
            [
                (f"{dip}: to 2012", oversold, 5, None, "2013-01-01"),
                (f"{dip}: 2013 to 2019", oversold, 5, "2013-01-01", recent),
                (f"{dip}: since {era}", oversold, 5, recent, None),
            ]
        ),
        "gap": rows(
            [(n, m, 20, None, None) for n, m in gaps]
            + [(f"{n}, since {era}", m, 20, recent, None) for n, m in gaps]
        ),
    }
