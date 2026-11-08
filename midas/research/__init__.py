"""Research: the backtesting and evaluation toolkit behind docs/results.md.

Everything here is deterministic code with no model calls. It exists so the
return engine can be measured before it is trusted:

- `data`:        aligned daily OHLCV bundles (yfinance download with a local
                 cache, an existing pickle, or a synthetic generator for tests)
- `portfolio`:   the drift-between-rebalances simulator with per-side costs
- `strategies`:  target-weight generators (trend filters, dual momentum,
                 risk parity, cross-sectional momentum, equal weight)
- `swing_clone`: the deterministic skeleton of the Midas swing committee
- `intraday`:    opening-range breakout and intraday-momentum backtests on
                 5-minute bars, plus the overnight/intraday decomposition
- `walkforward`: rolling out-of-sample parameter selection
- `metrics`:     CAGR, Sharpe against cash, drawdown, calendar years
- `report`:      renders the tables in docs/results.md

Conventions: signals are computed on a close and traded at the next day's
open; costs are basis points per side on turnover; cash earns the 13-week
T-bill yield; Sharpe is measured against that cash series.
"""
