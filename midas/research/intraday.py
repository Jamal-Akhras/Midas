"""Intraday rules on 5-minute bars, and the overnight/intraday split.

Bars are a DataFrame indexed by bar start time (tz-aware, exchange time)
with columns o, h, l, c, v. Only the regular session is used. Slippage in
basis points per side is the only cost: Alpaca charges no commission.

Rules implemented:
- intraday momentum (Gao, Han, Li & Zhou 2018): the first half-hour's sign
  predicts the last half-hour; trade 15:30-16:00 in that direction.
- first-candle opening-range breakout (Zarattini & Aziz 2023): go with the
  direction of the first 5-minute candle, stop at its far side, exit at the
  close; sized to risk a fixed fraction of equity, notional capped.
- range breakout: a 30-minute range, enter on a close outside it, stop at
  the range midpoint, exit at the close.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import numpy as np
import pandas as pd

EXCHANGE_TZ = "America/New_York"
SESSION_OPEN = "09:30"
LAST_BAR_START = "15:55"
Sizing = Literal["risk", "flat"]


def session_bars(bars: pd.DataFrame, tz: str = EXCHANGE_TZ) -> pd.DataFrame:
    """Regular-session bars in exchange time with `date` and half-hour `slot`
    (0 = 09:30-10:00 ... 12 = 15:30-16:00) columns."""
    df = bars.sort_index()
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    df = df.set_axis(idx.tz_convert(tz))
    df = df.between_time(SESSION_OPEN, LAST_BAR_START)
    df = df[~df.index.duplicated()]
    ts = pd.DatetimeIndex(df.index)
    df = df.assign(date=ts.date, slot=(ts.hour * 60 + ts.minute - 570) // 30)
    return df


def overnight_intraday_split(open_: pd.Series, close: pd.Series) -> pd.DataFrame:
    """Decompose close-to-close returns into the session (open->close) and
    the overnight gap (prior close->open). (1+intraday)(1+overnight) = 1+total."""
    intraday = close / open_ - 1.0
    overnight = open_ / close.shift(1) - 1.0
    total = close.pct_change()
    return pd.DataFrame({"intraday": intraday, "overnight": overnight, "total": total}).dropna()


def intraday_momentum(
    bars: pd.DataFrame, *, cost_bps: float = 1.0, require_penultimate_agreement: bool = False
) -> pd.DataFrame:
    """Per-day returns of the first-half-hour rule. `ret` is net of two sides
    of `cost_bps`; days with an incomplete session are skipped."""
    rows: list[tuple[Any, float, float, float, float, float]] = []
    for day, g in bars.groupby("date"):
        if g["slot"].nunique() < 13:
            continue
        hh = g.groupby("slot").agg(o=("o", "first"), c=("c", "last"))
        r1 = float(hh.loc[0, "c"] / hh.loc[0, "o"] - 1.0)
        r12 = float(hh.loc[11, "c"] / hh.loc[10, "c"] - 1.0)
        r13 = float(hh.loc[12, "c"] / hh.loc[11, "c"] - 1.0)
        signal = float(np.sign(r1))
        if require_penultimate_agreement and np.sign(r12) != signal:
            signal = 0.0
        ret = signal * r13 - abs(signal) * 2.0 * cost_bps / 1e4
        rows.append((day, r1, r12, r13, signal, ret))
    out = pd.DataFrame(rows, columns=["date", "r1", "r12", "r13", "signal", "ret"])
    return out.set_index("date")


def _walk_to_exit(
    o: np.ndarray,
    h: np.ndarray,
    lo: np.ndarray,
    c: np.ndarray,
    i0: int,
    side: int,
    stop: float,
    slip: float,
) -> float:
    """Exit price: the stop checked bar by bar from i0 (a gap through the
    stop exits at that bar's open), else the session's last close."""
    for i in range(i0, len(c)):
        if side == 1:
            if o[i] <= stop:
                return float(o[i]) * (1.0 - slip)
            if lo[i] <= stop:
                return stop * (1.0 - slip)
        else:
            if o[i] >= stop:
                return float(o[i]) * (1.0 + slip)
            if h[i] >= stop:
                return stop * (1.0 + slip)
    return float(c[-1]) * (1.0 - side * slip)


def _shares(
    equity: float,
    entry: float,
    risk_per_share: float,
    sizing: Sizing,
    risk_pct: float,
    max_leverage: float,
) -> int:
    if sizing == "risk":  # risk a fixed fraction of equity, notional capped at max_leverage x
        return int(min(equity * risk_pct / risk_per_share, equity * max_leverage / entry))
    return int(equity / entry)  # flat: 100% notional, no leverage


@dataclass(frozen=True)
class BreakoutParams:
    slippage_bps: float = 1.0
    sizing: Sizing = "risk"
    risk_pct: float = 0.01
    max_leverage: float = 4.0
    start_equity: float = 25_000.0


def _breakout_day(
    g: pd.DataFrame, equity: float, p: BreakoutParams, *, range_bars: int
) -> tuple[float, float, int]:
    """One session: (return on equity, R multiple, traded flag)."""
    o, h, lo, c = (g[k].to_numpy(dtype=float) for k in ("o", "h", "l", "c"))
    slip = p.slippage_bps / 1e4
    if range_bars == 1:  # first-candle rule: direction of the first bar, stop at its far side
        if c[0] == o[0]:
            return 0.0, 0.0, 0
        side = 1 if c[0] > o[0] else -1
        entry = float(o[1]) * (1.0 + side * slip)
        stop = float(lo[0]) if side == 1 else float(h[0])
        first_bar = 1
    else:  # range rule: first close outside the range, stop at the midpoint
        hi_r, lo_r = float(h[:range_bars].max()), float(lo[:range_bars].min())
        side, entry_bar = 0, -1
        for i in range(range_bars, len(c) - 1):
            if c[i] > hi_r:
                side, entry_bar = 1, i
                break
            if c[i] < lo_r:
                side, entry_bar = -1, i
                break
        if side == 0:
            return 0.0, 0.0, 0
        entry = float(c[entry_bar]) * (1.0 + side * slip)
        stop = (hi_r + lo_r) / 2.0
        first_bar = entry_bar + 1
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return 0.0, 0.0, 0
    shares = _shares(equity, entry, risk_per_share, p.sizing, p.risk_pct, p.max_leverage)
    if shares < 1:
        return 0.0, 0.0, 0
    exit_px = _walk_to_exit(o, h, lo, c, first_bar, side, stop, slip)
    pnl = side * (exit_px - entry) * shares
    return pnl / equity, side * (exit_px - entry) / risk_per_share, 1


def opening_range_breakout(
    bars: pd.DataFrame, params: BreakoutParams | None = None, *, range_bars: int = 1
) -> pd.DataFrame:
    """Per-day results (ret, r, traded) of the breakout rule; `range_bars=1`
    is the first-candle rule, 6 is a 30-minute range. Equity compounds."""
    p = params or BreakoutParams()
    equity = p.start_equity
    rows: list[tuple[Any, float, float, int]] = []
    for day, g in bars.groupby("date"):
        if len(g) < 30:
            continue
        ret, r, traded = _breakout_day(g, equity, p, range_bars=range_bars)
        rows.append((day, ret, r, traded))
        equity *= 1.0 + ret
        if equity <= 0:
            break
    return pd.DataFrame(rows, columns=["date", "ret", "r", "traded"]).set_index("date")


# ---------- data: Alpaca 5-minute bars with a local cache ----------


def fetch_alpaca_bars(
    symbol: str,
    *,
    api_key: str,
    api_secret: str,
    start: str = "2016-01-01",
    end: str | None = None,
    timeframe: str = "5Min",
    base_url: str = "https://data.alpaca.markets",
    feeds: tuple[str, ...] = ("sip", "iex"),
    page_limit: int = 10_000,
) -> pd.DataFrame:
    """All bars for `symbol`, split-adjusted, paged through next_page_token.
    Tries the consolidated feed first and falls back if the account lacks it.
    Returns a tz-aware frame (exchange time) with o, h, l, c, v, n, vw."""
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
    end_iso = (end or pd.Timestamp.utcnow().strftime("%Y-%m-%d")) + "T00:00:00Z"
    for feed in feeds:
        params: dict[str, Any] = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": f"{start}T00:00:00Z",
            "end": end_iso,
            "limit": page_limit,
            "adjustment": "split",
            "feed": feed,
        }
        rows: list[dict[str, Any]] = []
        denied = False
        while True:
            r = httpx.get(f"{base_url}/v2/stocks/bars", params=params, headers=headers, timeout=60)
            if r.status_code in (401, 403):
                denied = True
                break
            if r.status_code == 429:
                time.sleep(5)
                continue
            r.raise_for_status()
            body = r.json()
            rows.extend(body.get("bars", {}).get(symbol, []) or [])
            token = body.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        if denied:
            continue
        if not rows:
            return pd.DataFrame(columns=["o", "h", "l", "c", "v", "n", "vw"])
        df = pd.DataFrame(rows)
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("t"), utc=True)).tz_convert(EXCHANGE_TZ)
        return df[["o", "h", "l", "c", "v", "n", "vw"]].sort_index()
    raise PermissionError(f"no permitted data feed among {feeds}")


def cached_bars(cache_dir: Path, symbol: str) -> pd.DataFrame | None:
    path = cache_dir / f"{symbol}_5min.pkl"
    if not path.exists():
        return None
    frame: pd.DataFrame = pd.read_pickle(path)  # noqa: S301 - local cache we wrote
    return frame


def cache_bars(cache_dir: Path, symbol: str, bars: pd.DataFrame) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{symbol}_5min.pkl"
    bars.to_pickle(path)
    return path
