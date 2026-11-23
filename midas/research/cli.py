"""`midas research ...`: fetch data and rebuild docs/results.md.

midas research fetch [--out data/daily.pkl] [--start 2004-01-01]
midas research fetch-intraday --symbols SPY,QQQ [--out data/intraday]
midas research report [--data data/daily.pkl] [--out docs/results.md]
                      [--intraday data/intraday] [--no-clone]
midas research backtest [--years 10] [--data data/daily.pkl] [--capital 1000]
                        [--out FILE]   # the live configuration, replayed
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ETFS = [
    "SPY", "QQQ", "IWM", "EFA", "EEM", "AGG", "IEF", "TLT", "SHY", "BIL", "GLD", "DBC", "VNQ", "TIP",
    "MTUM", "QUAL", "USMV", "VLUE", "SPLV", "SPHQ", "JEPI", "JEPQ", "JHEQX", "GSLC",
]  # fmt: skip
SP500_SOURCE = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def sp500_symbols() -> list[str]:
    """Today's S&P 500 constituents (survivorship-biased by construction)."""
    import httpx
    import pandas as pd

    html = httpx.get(SP500_SOURCE, headers={"User-Agent": "midas-research/0.1"}, timeout=30).text
    table = pd.read_html(io.StringIO(html))[0]
    return [str(s).strip().replace(".", "-") for s in table["Symbol"]]


def cmd_fetch(args: argparse.Namespace) -> int:
    from midas.research.data import download

    tickers = set(ETFS) | set(args.extra)
    universes: dict[str, list[str]] = {"etfs": ETFS}
    if not args.no_sp500:
        sp = sp500_symbols()
        tickers |= set(sp)
        universes["sp500"] = sp
    print(f"downloading {len(tickers)} symbols from {args.start} ...", flush=True)
    data = download(sorted(tickers), args.start)
    data = type(data)(
        **{k: getattr(data, k) for k in ("open", "high", "low", "close", "volume", "cash_daily")},
        universes=universes,
    )
    data.to_pickle(Path(args.out))
    print(
        f"saved {args.out}: {data.close.shape[1]} symbols, {data.index[0].date()} to {data.index[-1].date()}"
    )
    return 0


def cmd_fetch_intraday(args: argparse.Namespace) -> int:
    from midas.config import settings
    from midas.research.intraday import cache_bars, fetch_alpaca_bars

    if not (settings.alpaca_api_key and settings.alpaca_api_secret):
        print("Alpaca keys are required (ALPACA_API_KEY / ALPACA_API_SECRET)", file=sys.stderr)
        return 2
    out = Path(args.out)
    for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        bars = fetch_alpaca_bars(
            symbol,
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            start=args.start,
            base_url=settings.alpaca_data_base_url,
        )
        path = cache_bars(out, symbol, bars)
        print(f"{symbol}: {len(bars)} bars -> {path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from midas.research.report import build

    text = build(
        Path(args.data),
        Path(args.out),
        intraday_dir=Path(args.intraday) if args.intraday else None,
        include_clone=not args.no_clone,
    )
    print(f"wrote {args.out} ({len(text.splitlines())} lines)")
    return 0


def live_account_config() -> Any:
    """The account as the live settings describe it, for `backtest`."""
    from midas.config import settings
    from midas.research.account import AccountConfig, SwingRules
    from midas.risk.engine import swing_budget_pct

    core_on = bool(settings.core_enabled)
    gate = bool(settings.setup_gate_enabled)
    rules = SwingRules(
        risk_pct=float(settings.per_trade_risk_pct),
        notional_cap_pct=float(settings.max_notional_pct_per_trade),
        max_holding_days=int(settings.max_holding_days),
        breakeven_r=float(settings.breakeven_at_r),
        trail_days=int(settings.trail_lookback_days),
        require_reclaim=gate and bool(settings.setup_gate_require_reclaim),
        stop_atr=float(settings.min_stop_atr),
        min_room=(
            float(settings.min_reward_to_risk) if gate and settings.setup_gate_require_room else 0.0
        ),
        entry_wait_days=5 if settings.entry_require_strength else 0,
        max_index_gap_pct=float(settings.entry_max_index_drop_pct),
        regime=bool(settings.regime_filter_enabled),
        throttle=float(settings.regime_throttle),
        trial_trades=int(settings.swing_trial_trades),
        committee_cap=int(settings.auto_cycle_max_committees),
    )
    return AccountConfig(
        core_universe=tuple(
            t.strip().upper() for t in settings.core_universe.split(",") if t.strip()
        ),
        core_allocation_pct=float(settings.core_allocation_pct) if core_on else 0.0,
        core_trend_window=int(settings.core_trend_window),
        cash_sleeve=bool(settings.core_cash_ticker.strip()),
        swing_budget_pct=float(swing_budget_pct()),
        swing=rules,
    )


def cmd_backtest(args: argparse.Namespace) -> int:
    import warnings

    import pandas as pd

    from midas.research.account import render, replay
    from midas.research.data import PriceData

    warnings.simplefilter("ignore", FutureWarning)
    data = PriceData.from_pickle(Path(args.data))
    start = data.index[-1] - pd.DateOffset(days=round(float(args.years) * 365.25))
    result = replay(data, live_account_config(), start=str(start.date()))
    text = render(
        result, capital=float(args.capital), committee_cost_usd=float(args.committee_cost)
    )
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="midas research",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download daily bars into a pickle cache")
    f.add_argument("--out", default="data/daily.pkl")
    f.add_argument("--start", default="2004-01-01")
    f.add_argument("--extra", nargs="*", default=[], help="additional tickers")
    f.add_argument("--no-sp500", action="store_true", help="skip the S&P 500 constituent list")
    f.set_defaults(fn=cmd_fetch)
    fi = sub.add_parser("fetch-intraday", help="download Alpaca 5-minute bars")
    fi.add_argument("--symbols", required=True)
    fi.add_argument("--out", default="data/intraday")
    fi.add_argument("--start", default="2016-01-01")
    fi.set_defaults(fn=cmd_fetch_intraday)
    r = sub.add_parser("report", help="rebuild docs/results.md")
    r.add_argument("--data", default="data/daily.pkl")
    r.add_argument("--out", default="docs/results.md")
    r.add_argument("--intraday", default="data/intraday")
    r.add_argument("--no-clone", action="store_true", help="skip the slow swing-clone section")
    r.set_defaults(fn=cmd_report)
    b = sub.add_parser("backtest", help="replay the live configuration over the last N years")
    b.add_argument("--years", type=float, default=10.0)
    b.add_argument("--data", default="data/daily.pkl")
    b.add_argument("--capital", type=float, default=1000.0, help="account size for the spend line")
    b.add_argument("--committee-cost", type=float, default=0.14, help="USD per committee")
    b.add_argument("--out", default="", help="also write the replay to this file")
    b.set_defaults(fn=cmd_backtest)
    args = parser.parse_args(argv)
    result: int = args.fn(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
