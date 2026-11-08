"""Builds docs/results.md: every strategy in the catalogue simulated on the
same data, summarised over the full window and the recent windows where the
cash hurdle is 4-5%, plus the swing-clone trade statistics, a walk-forward
check of the trend filter, and the intraday rules when 5-minute bars are
cached. Pure function of the data files, so the document is reproducible."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from midas.research import intraday, strategies
from midas.research.data import PriceData
from midas.research.metrics import Performance, summarize, trade_stats
from midas.research.portfolio import simulate
from midas.research.swing_clone import CloneStats, midas_clone
from midas.research.walkforward import walk_forward

WINDOWS = ("2020-01-01", "2022-01-01", "2024-01-01")
ETF_COST_BPS = 5.0
STOCK_COST_BPS = 10.0
SURVIVORSHIP = "survivorship bias: today's constituents"


@dataclass(frozen=True)
class Entry:
    name: str
    returns: pd.Series
    note: str = ""


def catalogue(data: PriceData) -> list[Entry]:
    """The strategies docs/results.md reports, given what the data holds."""
    rets = data.returns
    out: list[Entry] = []

    def sim(weights: pd.DataFrame, cost: float = ETF_COST_BPS) -> pd.Series:
        return simulate(weights, rets, data.cash_daily, cost_bps=cost)

    out.append(Entry("Cash (3m T-bill)", data.cash_daily, "savings-account proxy"))
    for t in ("SPY", "QQQ", "IWM", "EFA", "AGG", "TLT", "GLD"):
        if data.has(t):
            out.append(Entry(f"Buy & hold {t}", rets[t]))
    if data.has("SPY", "AGG"):
        out.append(
            Entry(
                "60/40 SPY/AGG, monthly", sim(strategies.fixed_mix(data, {"SPY": 0.6, "AGG": 0.4}))
            )
        )
    if data.has("SPY"):
        out.append(
            Entry(
                "SPY above 200d SMA, else T-bills (daily)",
                sim(strategies.sma_trend(data, "SPY")),
                "the Midas regime filter",
            )
        )
        out.append(
            Entry(
                "SPY 200d SMA half-throttle (50% when below)",
                sim(strategies.sma_trend(data, "SPY", below_weight=0.5)),
                "Midas regime_throttle=0.5",
            )
        )
        out.append(
            Entry("SPY 10-month SMA, monthly (Faber)", sim(strategies.sma_monthly(data, "SPY")))
        )
    if data.has("SPY", "EFA", "AGG"):
        out.append(Entry("GEM dual momentum (SPY/EFA/AGG)", sim(strategies.dual_momentum(data))))
    gtaa = ("SPY", "EFA", "IEF", "VNQ", "DBC")
    if data.has(*gtaa):
        out.append(
            Entry("GTAA-5 (Faber, 10m SMA, equal weight)", sim(strategies.tactical_sma(data, gtaa)))
        )
        out.append(
            Entry(
                "Ivy-5 equal weight, no timing",
                sim(strategies.fixed_mix(data, dict.fromkeys(gtaa, 0.2))),
            )
        )
    if data.has("SPY", "TLT", "IEF", "GLD", "DBC"):
        out.append(
            Entry(
                "All Weather (30/40/15/7.5/7.5)",
                sim(
                    strategies.fixed_mix(
                        data, {"SPY": 0.3, "TLT": 0.4, "IEF": 0.15, "GLD": 0.075, "DBC": 0.075}
                    )
                ),
            )
        )
    if data.has("SPY", "TLT", "GLD", "SHY"):
        out.append(
            Entry(
                "Permanent Portfolio",
                sim(
                    strategies.fixed_mix(data, {"SPY": 0.25, "TLT": 0.25, "GLD": 0.25, "SHY": 0.25})
                ),
            )
        )
    if data.has("SPY", "TLT", "GLD", "DBC"):
        out.append(
            Entry(
                "Risk parity, inverse vol (SPY/TLT/GLD/DBC)",
                sim(strategies.inverse_vol(data, ("SPY", "TLT", "GLD", "DBC"))),
            )
        )
    for t in ("MTUM", "QUAL", "USMV", "VLUE", "GSLC", "JEPI", "JHEQX"):
        if data.has(t):
            out.append(Entry(f"Fund {t}", rets[t]))
    sp500 = data.available(data.universes.get("sp500", []))
    if data.has("SPY") and len(sp500) >= 100:
        out.append(
            Entry(
                "XS momentum 12-1, top 50 of S&P 500, monthly",
                sim(
                    strategies.cross_sectional(data, sp500, strategies.momentum_12_1),
                    STOCK_COST_BPS,
                ),
                SURVIVORSHIP,
            )
        )
        out.append(
            Entry(
                "XS momentum top 50 + SPY 200d filter",
                sim(
                    strategies.cross_sectional(
                        data, sp500, strategies.momentum_12_1, trend_filter=("SPY", 200)
                    ),
                    STOCK_COST_BPS,
                ),
                SURVIVORSHIP,
            )
        )
        out.append(
            Entry(
                "Low vol: 50 least volatile of S&P 500",
                sim(
                    strategies.cross_sectional(
                        data, sp500, strategies.realized_vol, ascending=True
                    ),
                    STOCK_COST_BPS,
                ),
                SURVIVORSHIP,
            )
        )
        out.append(
            Entry(
                "Equal-weight S&P 500 (today's list)",
                sim(strategies.equal_weight(data, sp500), STOCK_COST_BPS),
                SURVIVORSHIP + "; gauges the bias vs SPY",
            )
        )
    return out


def performance_table(
    entries: Sequence[Entry], data: PriceData, *, since: str | None = None
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for e in entries:
        r = e.returns if since is None else e.returns[e.returns.index >= pd.Timestamp(since)]
        if since is not None and (
            r.empty or r.index[0] > pd.Timestamp(since) + pd.Timedelta(days=10)
        ):
            continue  # not alive at the window start
        p = summarize(r, e.name, cash=data.cash_daily, note=e.note)
        if p is not None:
            rows.append(p.as_row())
    return pd.DataFrame(rows)


def clone_table(
    data: PriceData, universe: Sequence[str]
) -> tuple[pd.DataFrame, dict[str, CloneStats]]:
    variants: list[tuple[str, dict[str, Any]]] = [
        ("Midas clone: 5% stop, 15-session time stop, regime 0.5 (defaults)", {}),
        ("Midas clone: no time stop (trail only)", {"time_stop_days": 0}),
        ("Midas clone: 8% stop, no time stop", {"stop_pct": 0.08, "time_stop_days": 0}),
        ("Midas clone: no regime filter", {"regime": False}),
        ("Midas clone: 2% risk per trade", {"risk_pct": 2.0}),
        ("Midas clone: 1 entry/day, score >= 85", {"max_new_per_day": 1, "score_min": 85.0}),
    ]
    rows: list[dict[str, Any]] = []
    stats: dict[str, CloneStats] = {}
    for name, kw in variants:
        ret, st = midas_clone(data, universe, **kw)
        p = summarize(
            ret, name, cash=data.cash_daily, note="deterministic skeleton, no LLM; " + SURVIVORSHIP
        )
        if p is not None:
            rows.append(
                {**p.as_row(), **{f"trade_{k}": v for k, v in st.as_row().items() if k != "exits"}}
            )
            stats[name] = st
    return pd.DataFrame(rows), stats


def walkforward_table(data: PriceData) -> tuple[pd.DataFrame, pd.DataFrame]:
    """SMA window chosen on the trailing 5 years vs the fixed 200-day rule."""
    grid = [{"window": w} for w in (100, 150, 200, 250, 300)]
    wf = walk_forward(data, lambda d, **kw: strategies.sma_trend(d, "SPY", **kw), grid)
    rows: list[Performance] = []
    oos = wf.oos_returns
    if not oos.empty:
        start = oos.index[0]
        for label, series in wf.candidates.items():
            p = summarize(series[series.index >= start], f"fixed {label}", cash=data.cash_daily)
            if p is not None:
                rows.append(p)
        p = summarize(oos, "walk-forward (5y train, 1y test)", cash=data.cash_daily)
        if p is not None:
            rows.append(p)
        spy = data.returns["SPY"]
        p = summarize(spy[spy.index >= start], "Buy & hold SPY (same window)", cash=data.cash_daily)
        if p is not None:
            rows.append(p)
    return pd.DataFrame([r.as_row() for r in rows]), wf.choices


def intraday_table(
    bars_by_symbol: dict[str, pd.DataFrame], slippages: Sequence[float] = (0.0, 0.5, 1.0, 2.0)
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    def add(name: str, symbol: str, ret: pd.Series, r: pd.Series | None = None) -> None:
        p = summarize(ret, name)
        if p is None:
            return
        ts = trade_stats(ret, r)
        rows.append(
            {
                "symbol": symbol,
                **p.as_row(),
                **{k: v for k, v in ts.as_row().items() if k != "days"},
            }
        )

    for symbol, raw in bars_by_symbol.items():
        bars = intraday.session_bars(raw)
        daily = bars.groupby("date").agg(o=("o", "first"), c=("c", "last"))
        add("Buy & hold (close to close)", symbol, daily["c"].pct_change())
        add("Long open-to-close only, no cost", symbol, daily["c"] / daily["o"] - 1.0)
        add(
            "Intraday momentum, zero cost",
            symbol,
            intraday.intraday_momentum(bars, cost_bps=0.0)["ret"],
        )
        add(
            "Intraday momentum, 1 bp/side",
            symbol,
            intraday.intraday_momentum(bars, cost_bps=1.0)["ret"],
        )
        for slip in slippages:
            for sizing in ("risk", "flat"):
                label = "4x, 1% risk" if sizing == "risk" else "unlevered"
                res = intraday.opening_range_breakout(
                    bars, intraday.BreakoutParams(slippage_bps=slip, sizing=sizing)
                )
                add(
                    f"ORB first 5-min candle, {label}, {slip:g} bp/side",
                    symbol,
                    res["ret"],
                    res.loc[res["traded"] == 1, "r"],
                )
        res = intraday.opening_range_breakout(
            bars, intraday.BreakoutParams(slippage_bps=1.0, sizing="flat"), range_bars=6
        )
        add(
            "ORB 30-min range, midpoint stop, unlevered, 1 bp/side",
            symbol,
            res["ret"],
            res.loc[res["traded"] == 1, "r"],
        )
    return pd.DataFrame(rows)


def overnight_table(bars_by_symbol: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol, raw in bars_by_symbol.items():
        bars = intraday.session_bars(raw)
        daily = bars.groupby("date").agg(o=("o", "first"), c=("c", "last"))
        split = intraday.overnight_intraday_split(daily["o"], daily["c"])
        for leg in ("total", "overnight", "intraday"):
            p = summarize(split[leg], leg)
            if p is not None:
                rows.append(
                    {
                        "symbol": symbol,
                        "leg": leg,
                        "cagr": p.cagr,
                        "sharpe": p.sharpe,
                        "max_drawdown": p.max_drawdown,
                    }
                )
    return pd.DataFrame(rows)


# ---------- rendering ----------

PCT_COLS = (
    "cagr",
    "cash_cagr",
    "vol",
    "max_drawdown",
    "worst_year",
    "pct_years_beat_cash",
    "win_rate",
)


def _fmt(frame: pd.DataFrame, columns: Sequence[str]) -> str:
    if frame.empty:
        return "_no data_\n"
    f = frame.copy()
    for c in f.columns:
        if c in PCT_COLS:
            f[c] = f[c].map(lambda x: f"{x * 100:.1f}%" if pd.notna(x) else "")
        elif c in ("sharpe", "trade_avg_r", "avg_r", "trade_profit_factor", "train_score"):
            f[c] = f[c].map(lambda x: f"{x:.2f}" if pd.notna(x) else "")
        elif c in ("growth_of_1k",):
            f[c] = f[c].map(lambda x: f"{x:,.0f}")
        elif c in ("avg_trade_bps", "trade_avg_days", "trade_win_rate"):
            f[c] = f[c].map(lambda x: f"{x:.2f}" if pd.notna(x) else "")
    cols = [c for c in columns if c in f.columns]
    f = f[cols]
    header = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = "".join(
        "| " + " | ".join(str(v) for v in row) + " |\n" for row in f.itertuples(index=False)
    )
    return header + body


FULL_COLS = (
    "name",
    "start",
    "cagr",
    "vol",
    "sharpe",
    "max_drawdown",
    "worst_year",
    "pct_years_beat_cash",
    "growth_of_1k",
    "note",
)
WINDOW_COLS = ("name", "cagr", "cash_cagr", "sharpe", "max_drawdown")
CLONE_COLS = (
    "name",
    "cagr",
    "sharpe",
    "max_drawdown",
    "trade_n_trades",
    "trade_win_rate",
    "trade_avg_r",
    "trade_profit_factor",
    "trade_avg_days",
)
WF_COLS = ("name", "start", "cagr", "sharpe", "max_drawdown")
INTRADAY_COLS = (
    "symbol",
    "name",
    "cagr",
    "sharpe",
    "max_drawdown",
    "trades",
    "win_rate",
    "avg_trade_bps",
    "avg_r",
)


def render(
    data: PriceData,
    *,
    bars_by_symbol: dict[str, pd.DataFrame] | None = None,
    include_clone: bool = True,
    windows: Sequence[str] = WINDOWS,
    data_label: str = "data/daily.pkl",
) -> str:
    entries = catalogue(data)
    full = performance_table(entries, data)
    end = str(data.index[-1].date())
    parts: list[str] = [
        "# Results\n",
        f"Generated by `midas research report` from `{data_label}` (daily bars "
        f"{data.index[0].date()} to {end}). Do not edit by hand; rerun the command.\n",
        "## Method\n",
        "- Adjusted daily closes from yfinance. Cash is the 13-week T-bill yield (^IRX); "
        "Sharpe is excess over that cash series, so holding T-bills scores 0.\n"
        f"- Costs: {ETF_COST_BPS:g} bp per side on ETFs, {STOCK_COST_BPS:g} bp on single stocks, charged on turnover. "
        "Monthly strategies read the month-end close and trade the next open; daily filters trade only when the signal flips.\n"
        "- Cross-sectional rows use today's S&P 500 list and therefore carry survivorship bias; "
        "the equal-weight row measures that bias against SPY.\n"
        "- The Midas clone is the committee's deterministic rule set with the LLM's stock picking replaced by the scored screen. "
        "It measures the framing (tight stops, time stops, throttled exposure), not any model.\n",
        f"## Full window (to {end})\n",
        _fmt(full, FULL_COLS),
    ]
    for w in windows:
        parts.append(f"\n## Since {w} (cash hurdle at today's rates)\n")
        parts.append(_fmt(performance_table(entries, data, since=w), WINDOW_COLS))
    if include_clone:
        sp500 = data.available(data.universes.get("sp500", []))
        if data.has("SPY") and len(sp500) >= 100:
            clone, _ = clone_table(data, sp500)
            parts.append("\n## The Midas swing skeleton (S&P 500 universe, from 2007)\n")
            parts.append(_fmt(clone, CLONE_COLS))
            for w in windows:
                parts.append(f"\nSince {w}:\n\n")
                parts.append(
                    _fmt(
                        performance_table(
                            [Entry(n, r) for n, r in _clone_series(data, sp500)], data, since=w
                        ),
                        WINDOW_COLS,
                    )
                )
    if data.has("SPY"):
        wf, choices = walkforward_table(data)
        parts.append("\n## Walk-forward check of the trend filter\n")
        parts.append(
            "SMA window picked each year from {100,150,200,250,300} by trailing 5-year Sharpe, applied to the next year, versus holding each window fixed over the same period.\n\n"
        )
        parts.append(_fmt(wf, WF_COLS))
        if not choices.empty:
            parts.append("\nChosen each year:\n\n")
            parts.append(_fmt(choices, ("test_start", "test_end", "chosen", "train_score")))
    if bars_by_symbol:
        parts.append(
            "\n## Intraday (Alpaca 5-minute bars, regular session, slippage is the only cost)\n"
        )
        parts.append("Where the equity return accrues:\n\n")
        parts.append(
            _fmt(
                overnight_table(bars_by_symbol), ("symbol", "leg", "cagr", "sharpe", "max_drawdown")
            )
        )
        parts.append("\nRules:\n\n")
        parts.append(_fmt(intraday_table(bars_by_symbol), INTRADAY_COLS))
    return "".join(parts)


def _clone_series(data: PriceData, universe: Sequence[str]) -> list[tuple[str, pd.Series]]:
    ret, _ = midas_clone(data, universe)
    return [("Midas clone (defaults)", ret)]


def build(
    data_path: Path,
    out_path: Path,
    *,
    intraday_dir: Path | None = None,
    include_clone: bool = True,
    loader: Callable[[Path], PriceData] = PriceData.from_pickle,
) -> str:
    data = loader(data_path)
    bars: dict[str, pd.DataFrame] = {}
    if intraday_dir is not None and intraday_dir.exists():
        for path in sorted(intraday_dir.glob("*_5min.pkl")):
            symbol = path.name.split("_")[0]
            cached = intraday.cached_bars(intraday_dir, symbol)
            if cached is not None:
                bars[symbol] = cached
    text = render(
        data, bars_by_symbol=bars or None, include_clone=include_clone, data_label=str(data_path)
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return text
