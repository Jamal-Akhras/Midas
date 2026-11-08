"""Price data for research: aligned daily OHLCV frames plus a cash-return
series. Pickle format (the cache) is a dict with keys open/high/low/close/
volume holding DataFrames indexed by date with one column per ticker, and
optional list-valued keys such as "sp500" describing universes."""

from __future__ import annotations

import pickle
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

FRAME_KEYS = ("open", "high", "low", "close", "volume")
CASH_SYMBOL = "^IRX"  # 13-week T-bill discount yield, in percent
TRADING_DAYS = 252


@dataclass(frozen=True)
class PriceData:
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    cash_daily: pd.Series  # daily return of the cash proxy, aligned to close.index
    universes: Mapping[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        idx = self.close.index
        for key in FRAME_KEYS:
            frame = getattr(self, key)
            if not frame.index.equals(idx):
                raise ValueError(f"{key} index is not aligned with close")
        if not self.cash_daily.index.equals(idx):
            raise ValueError("cash_daily index is not aligned with close")

    @property
    def index(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.close.index)

    @property
    def tickers(self) -> list[str]:
        return [str(c) for c in self.close.columns]

    @property
    def returns(self) -> pd.DataFrame:
        return self.close.pct_change()

    def has(self, *tickers: str) -> bool:
        return all(t in self.close.columns for t in tickers)

    def available(self, tickers: Iterable[str], min_rows: int = 300) -> list[str]:
        """Tickers present with at least `min_rows` non-missing closes."""
        counts = self.close.notna().sum()
        return [t for t in tickers if t in counts.index and int(counts[t]) >= min_rows]

    def since(self, start: str | pd.Timestamp) -> PriceData:
        mask = self.close.index >= pd.Timestamp(start)
        return PriceData(
            open=self.open.loc[mask],
            high=self.high.loc[mask],
            low=self.low.loc[mask],
            close=self.close.loc[mask],
            volume=self.volume.loc[mask],
            cash_daily=self.cash_daily.loc[mask],
            universes=self.universes,
        )

    def to_pickle(self, path: Path) -> None:
        payload: dict[str, Any] = {k: getattr(self, k) for k in FRAME_KEYS}
        payload["cash_daily"] = self.cash_daily
        payload.update(dict(self.universes))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            pickle.dump(payload, fh)

    @classmethod
    def from_pickle(cls, path: Path) -> PriceData:
        with path.open("rb") as fh:
            raw: dict[str, Any] = pickle.load(fh)  # noqa: S301 - local cache we wrote
        return cls.from_frames(raw)

    @classmethod
    def from_frames(cls, raw: Mapping[str, Any]) -> PriceData:
        frames = {k: raw[k].sort_index() for k in FRAME_KEYS}
        idx = frames["close"].index
        frames = {k: v.reindex(idx) for k, v in frames.items()}
        for k, v in frames.items():
            frames[k] = v.loc[:, ~v.columns.duplicated()]
        cash = raw.get("cash_daily")
        if cash is None:
            if CASH_SYMBOL in frames["close"].columns:
                cash = cash_from_tbill_yield(frames["close"][CASH_SYMBOL], idx)
            else:
                cash = pd.Series(0.0, index=idx, name="CASH")
        universes = {
            k: [str(t) for t in v]
            for k, v in raw.items()
            if k not in FRAME_KEYS and k != "cash_daily" and isinstance(v, list)
        }
        return cls(cash_daily=cash.reindex(idx).fillna(0.0), universes=universes, **frames)


def cash_from_tbill_yield(yield_pct: pd.Series, index: pd.Index) -> pd.Series:
    """Daily cash return from a T-bill discount yield quoted in percent
    (e.g. ^IRX). Forward-filled, floored at zero."""
    y = yield_pct.reindex(index).ffill().bfill().clip(lower=0.0) / 100.0
    daily = (1.0 + y) ** (1.0 / TRADING_DAYS) - 1.0
    daily.name = "CASH"
    return daily


def synthetic(
    tickers: Iterable[str],
    days: int = 1500,
    *,
    seed: int = 0,
    annual_drift: float = 0.08,
    annual_vol: float = 0.20,
    cash_rate: float = 0.02,
    start: str = "2010-01-04",
) -> PriceData:
    """Geometric Brownian motion prices on a business-day calendar, with a
    constant cash rate. Deterministic for a given seed; for tests only."""
    tickers = list(tickers)
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=days)
    mu = annual_drift / TRADING_DAYS
    sigma = annual_vol / np.sqrt(TRADING_DAYS)
    shocks = rng.normal(mu - 0.5 * sigma**2, sigma, size=(days, len(tickers)))
    close = pd.DataFrame(100.0 * np.exp(np.cumsum(shocks, axis=0)), index=idx, columns=tickers)
    open_ = close.shift(1).fillna(100.0)
    high = pd.concat([open_, close]).groupby(level=0).max() * 1.005
    low = pd.concat([open_, close]).groupby(level=0).min() * 0.995
    volume = pd.DataFrame(1_000_000.0, index=idx, columns=tickers)
    cash = pd.Series((1 + cash_rate) ** (1 / TRADING_DAYS) - 1, index=idx, name="CASH")
    return PriceData(
        open=open_, high=high, low=low, close=close, volume=volume, cash_daily=cash, universes={}
    )


def download(
    tickers: Iterable[str], start: str = "2004-01-01", *, cash_symbol: str = CASH_SYMBOL
) -> PriceData:
    """Download adjusted daily bars from yfinance (needs the `research` extra
    and network access). The cash symbol is added automatically."""
    import yfinance as yf  # imported here: optional dependency

    symbols = sorted({*tickers, cash_symbol})
    frames: dict[str, list[pd.DataFrame]] = {}
    for i in range(0, len(symbols), 120):
        chunk = symbols[i : i + 120]
        df = yf.download(
            chunk, start=start, auto_adjust=True, progress=False, threads=True, group_by="column"
        )
        for field_name in ("Open", "High", "Low", "Close", "Volume"):
            if isinstance(df.columns, pd.MultiIndex):
                part = df[field_name].copy()
            else:
                part = df[[field_name]].rename(columns={field_name: chunk[0]})
            frames.setdefault(field_name.lower(), []).append(part)
    raw: dict[str, Any] = {k: pd.concat(v, axis=1).sort_index() for k, v in frames.items()}
    return PriceData.from_frames(raw)


def load_or_download(cache: Path, tickers: Iterable[str], start: str = "2004-01-01") -> PriceData:
    """The cached bundle if present, otherwise download and cache it."""
    if cache.exists():
        return PriceData.from_pickle(cache)
    data = download(tickers, start)
    data.to_pickle(cache)
    return data
