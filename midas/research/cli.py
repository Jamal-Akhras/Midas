"""`midas research ...`: fetch data and rebuild docs/results.md.

midas research fetch [--out data/daily.pkl] [--start 2004-01-01]
midas research fetch-intraday --symbols SPY,QQQ [--out data/intraday]
midas research report [--data data/daily.pkl] [--out docs/results.md]
                      [--intraday data/intraday] [--no-clone]
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from pathlib import Path

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
    args = parser.parse_args(argv)
    result: int = args.fn(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
