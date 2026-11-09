"""Market data + options chains. Provider order: Alpaca (paper keys, free)
-> yfinance (no key) -> demo fixtures. Source is always reported honestly."""

import logging
import re
from typing import Any

import httpx

from midas.config import settings
from midas.tools import demo_data
from midas.tools.base import ToolError, ToolResult

logger = logging.getLogger(__name__)


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }


def _alpaca_enabled() -> bool:
    return bool(settings.alpaca_api_key and settings.alpaca_api_secret)


_sector_cache: dict[str, str] = {}


def _finnhub_sector(ticker: str) -> str:
    """Industry classification from Finnhub's profile (cached; sectors
    do not change intraday). Empty when unavailable - the concentration
    check must degrade to inert honestly, not crash quotes."""
    if ticker in _sector_cache:
        return _sector_cache[ticker]
    sector = ""
    if settings.finnhub_api_key:
        try:
            r = httpx.get(
                "https://finnhub.io/api/v1/stock/profile2",
                params={"symbol": ticker, "token": settings.finnhub_api_key},
                timeout=10,
            )
            if r.status_code == 200:
                sector = r.json().get("finnhubIndustry") or ""
        except Exception:
            pass
    _sector_cache[ticker] = sector
    return sector


def _yf():
    try:
        import yfinance

        return yfinance
    except ImportError:
        return None


def get_quote(ticker: str) -> ToolResult:
    """Latest price, previous close, average volume, sector."""
    ticker = ticker.upper()
    if settings.effective_demo_mode:
        q = demo_data.QUOTES.get(ticker)
        if q is None:
            raise ToolError(
                f"no demo quote for {ticker} (demo universe: {demo_data.DEMO_UNIVERSE})"
            )
        return ToolResult(data={"ticker": ticker, **q}, source="demo")
    if _alpaca_enabled():
        try:
            r = httpx.get(
                f"{settings.alpaca_data_base_url}/v2/stocks/{ticker}/trades/latest",
                headers=_alpaca_headers(),
                timeout=15,
            )
            r.raise_for_status()
            price = r.json()["trade"]["p"]
            bars = _alpaca_bars(ticker, days=30)
            if bars:  # empty bars => zeros would poison the floor check;
                # fall through to yfinance instead of lying
                avg_vol = sum(b["volume"] for b in bars) / len(bars)
                prev_close = bars[-1]["close"]
                if (
                    bars[-1]["date"] == __import__("datetime").date.today().isoformat()
                    and len(bars) > 1
                ):
                    prev_close = bars[-2]["close"]
                return ToolResult(
                    data={
                        "ticker": ticker,
                        "price": price,
                        "prev_close": prev_close,
                        "avg_volume": avg_vol,
                        "sector": _finnhub_sector(ticker),
                    },
                    source="alpaca",
                )
            logger.warning("alpaca returned no bars for %s; trying yfinance", ticker)
        except httpx.HTTPError as exc:
            logger.warning("alpaca quote failed for %s: %s", ticker, type(exc).__name__)
    yf = _yf()
    if yf is not None:
        try:
            info = yf.Ticker(ticker).fast_info
            return ToolResult(
                data={
                    "ticker": ticker,
                    "price": float(info["lastPrice"]),
                    "prev_close": float(info["previousClose"]),
                    "avg_volume": float(info.get("threeMonthAverageVolume") or 0),
                    "sector": "",
                },
                source="yfinance",
            )
        except Exception as exc:  # yfinance raises many types
            logger.warning("yfinance quote failed for %s: %s", ticker, type(exc).__name__)
    raise ToolError(f"no market data source available for {ticker}")


def _alpaca_bars(ticker: str, days: int) -> list[dict]:
    """The newest `days` daily bars from Alpaca, oldest first.

    Alpaca serves bars oldest-first from `start` and caps each page at
    `limit`, so `limit=days` over a wide window returns the OLDEST bars in it
    and stops weeks before today; the first core plan of November 2026 sized
    its trend filter on July closes that way. We page to the end and keep the
    tail. An explicit start is still required: without it Alpaca returns only
    today's bar, an empty list pre-market. `end` sits 16 minutes in the past
    because the free plan serves the consolidated (sip) feed only for data
    older than 15 minutes; iex stays as the fallback (its volume undercounts
    30-50x).
    """
    from datetime import date, datetime, timedelta, timezone

    start = (date.today() - timedelta(days=int(days * 1.6) + 10)).isoformat()
    end = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for feed in ("sip", "iex"):
        raw: list[dict] = []
        page_token: str | None = None
        denied = False
        while True:
            params: dict[str, Any] = {
                "timeframe": "1Day",
                "limit": 10000,
                "adjustment": "split",
                "start": start,
                "end": end,
                "feed": feed,
            }
            if page_token:
                params["page_token"] = page_token
            r = httpx.get(
                f"{settings.alpaca_data_base_url}/v2/stocks/{ticker}/bars",
                params=params,
                headers=_alpaca_headers(),
                timeout=15,
            )
            if r.status_code in (401, 403) and feed == "sip":
                denied = True
                break
            r.raise_for_status()
            body = r.json()
            raw.extend(body.get("bars") or [])
            page_token = body.get("next_page_token")
            if not page_token:
                break
        if denied:
            continue
        return [
            {
                "date": b["t"][:10],
                "open": b["o"],
                "high": b["h"],
                "low": b["l"],
                "close": b["c"],
                "volume": b["v"],
            }
            for b in raw[-days:]
        ]
    return []


def get_daily_bars(ticker: str, days: int = 60) -> ToolResult:
    """Daily OHLCV bars, most recent last."""
    ticker = ticker.upper()
    days = min(int(days), 250)
    if settings.effective_demo_mode:
        return ToolResult(data=demo_data.daily_bars(ticker, days), source="demo")
    if _alpaca_enabled():
        try:
            return ToolResult(data=_alpaca_bars(ticker, days), source="alpaca")
        except httpx.HTTPError as exc:
            logger.warning("alpaca bars failed for %s: %s", ticker, type(exc).__name__)
    yf = _yf()
    if yf is not None:
        try:
            hist = yf.Ticker(ticker).history(period=f"{days}d")
            bars = [
                {
                    "date": str(idx.date()),
                    "open": round(float(row["Open"]), 4),
                    "high": round(float(row["High"]), 4),
                    "low": round(float(row["Low"]), 4),
                    "close": round(float(row["Close"]), 4),
                    "volume": int(row["Volume"]),
                }
                for idx, row in hist.iterrows()
            ]
            return ToolResult(data=bars, source="yfinance")
        except Exception as exc:
            logger.warning("yfinance bars failed for %s: %s", ticker, type(exc).__name__)
    raise ToolError(f"no bar data source available for {ticker}")


_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _occ_meta(symbol: str) -> dict | None:
    """Parse an OCC option symbol: ORCL260918C00150000 ->
    expiry 2026-09-18, type call, strike 150.0."""
    m = _OCC.match(symbol)
    if not m:
        return None
    _, ymd, cp, strike = m.groups()
    return {
        "expiry": f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}",
        "type": "call" if cp == "C" else "put",
        "strike": int(strike) / 1000.0,
    }


def _chain_from_snapshots(snapshots: dict, spot: float) -> list[dict]:
    """Build our chain rows from Alpaca option snapshots (indicative feed)."""
    from datetime import date

    chain: list[dict] = []
    today = date.today()
    for symbol, s in (snapshots or {}).items():
        meta = _occ_meta(symbol)
        if meta is None:
            continue
        if spot and abs(meta["strike"] - spot) / spot > 0.15:
            continue  # keep chains tractable
        quote = s.get("latestQuote") or {}
        bid = _num(quote.get("bp"))
        ask = _num(quote.get("ap"))
        greeks = s.get("greeks") or {}
        try:
            dte = (date.fromisoformat(meta["expiry"]) - today).days
        except ValueError:
            continue
        if dte < 0 or dte > 120:
            continue
        chain.append(
            {
                "expiry": meta["expiry"],
                "dte": dte,
                "type": meta["type"],
                "strike": meta["strike"],
                "bid": bid,
                "ask": ask,
                "mid": round((bid + ask) / 2, 4),
                "open_interest": int(_num(s.get("openInterest") or s.get("open_interest"))),
                "volume": int(_num((s.get("dailyBar") or {}).get("v"))),
                "iv": round(_num(s.get("impliedVolatility")), 4),
                "delta": (round(_num(greeks.get("delta")), 4) if greeks else None),
            }
        )
    return chain


def _alpaca_option_chain(ticker: str) -> list[dict]:
    quote = get_quote(ticker).data
    spot = float(quote["price"])
    snapshots: dict = {}
    params: dict[str, Any] = {"feed": "indicative", "limit": 1000}
    for _ in range(4):  # a few pages is plenty inside the +-15% band
        r = httpx.get(
            f"{settings.alpaca_data_base_url}/v1beta1/options/snapshots/{ticker}",
            params=params,
            headers=_alpaca_headers(),
            timeout=20,
        )
        r.raise_for_status()
        body = r.json()
        snapshots.update(body.get("snapshots") or {})
        token = body.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    chain = _chain_from_snapshots(snapshots, spot)
    if chain and not any(c["open_interest"] > 0 for c in chain):
        # feed returned no OI: every contract would fail the liquidity
        # floor for the wrong reason - let yfinance try instead
        logger.warning("alpaca chain for %s has no open interest; falling back", ticker)
        return []
    return chain


def _num(v, default=0.0) -> float:
    """NaN/None-safe float: yfinance reports NaN for illiquid contracts,
    and int(NaN) raises ValueError."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else f


def get_options_chain(ticker: str) -> ToolResult:
    """Options chain with bid/ask, open interest, volume, IV and delta where
    available. Structurer hard-checks run on this data."""
    ticker = ticker.upper()
    if settings.effective_demo_mode:
        return ToolResult(data=demo_data.options_chain(ticker), source="demo")
    if _alpaca_enabled():
        try:
            chain = _alpaca_option_chain(ticker)
            if chain:
                return ToolResult(data=chain, source="alpaca")
        except Exception as exc:
            logger.warning("alpaca chain failed for %s: %s", ticker, type(exc).__name__)
    yf = _yf()
    if yf is not None:
        try:
            t = yf.Ticker(ticker)
            spot = float(t.fast_info["lastPrice"])
            chain = []
            from datetime import date

            for expiry in t.options[:8]:
                dte = (date.fromisoformat(expiry) - date.today()).days
                oc = t.option_chain(expiry)
                for opt_type, frame in (("call", oc.calls), ("put", oc.puts)):
                    for _, row in frame.iterrows():
                        strike = float(row["strike"])
                        if abs(strike - spot) / spot > 0.15:
                            continue  # keep chains tractable
                        bid = _num(row.get("bid"))
                        ask = _num(row.get("ask"))
                        chain.append(
                            {
                                "expiry": expiry,
                                "dte": dte,
                                "type": opt_type,
                                "strike": strike,
                                "bid": bid,
                                "ask": ask,
                                "mid": round((bid + ask) / 2, 4),
                                "open_interest": int(_num(row.get("openInterest"))),
                                "volume": int(_num(row.get("volume"))),
                                "iv": round(_num(row.get("impliedVolatility")), 4),
                                "delta": None,  # yfinance has no greeks
                            }
                        )
            return ToolResult(data=chain, source="yfinance")
        except Exception as exc:
            logger.warning("yfinance chain failed for %s: %s", ticker, type(exc).__name__)
    raise ToolError(f"no options chain source available for {ticker}")
