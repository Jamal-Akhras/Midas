"""Earnings history, analyst estimates, and news.

Providers: Finnhub or FMP free tier (compare during M1 and pick one - both
are wired; Finnhub is tried first). Demo fixtures otherwise.
"""
import logging

import httpx

from midas.config import settings
from midas.tools import demo_data
from midas.tools.base import ToolError, ToolResult

logger = logging.getLogger(__name__)


def get_earnings(ticker: str) -> ToolResult:
    """Earnings history (actual vs estimate), next earnings date, analyst
    consensus estimates, key ratios."""
    ticker = ticker.upper()
    if settings.effective_demo_mode:
        return ToolResult(data={"ticker": ticker, **demo_data.EARNINGS["default"]}, source="demo")
    if not (settings.finnhub_api_key or settings.fmp_api_key):
        raise ToolError(
            "no earnings data source configured (set FINNHUB_API_KEY or "
            "FMP_API_KEY - both have free tiers); earnings claims must go "
            "in 'unverified'")
    if settings.finnhub_api_key:
        try:
            return ToolResult(data=_finnhub_earnings(ticker), source="finnhub")
        except httpx.HTTPError as exc:
            logger.warning("finnhub earnings failed: %s", type(exc).__name__)
    if settings.fmp_api_key:
        try:
            return ToolResult(data=_fmp_earnings(ticker), source="fmp")
        except httpx.HTTPError as exc:
            logger.warning("fmp earnings failed: %s", type(exc).__name__)
    raise ToolError(f"no earnings data source succeeded for {ticker}")


def _finnhub_earnings(ticker: str) -> dict:
    base = "https://finnhub.io/api/v1"
    key = {"token": settings.finnhub_api_key}
    surprises = httpx.get(f"{base}/stock/earnings", params={"symbol": ticker, **key},
                          timeout=15)
    surprises.raise_for_status()
    quarters = [
        {"period": s.get("period", ""), "eps_actual": s.get("actual"),
         "eps_estimate": s.get("estimate"),
         "surprise_pct": s.get("surprisePercent")}
        for s in surprises.json()[:8]
    ]
    metrics = httpx.get(f"{base}/stock/metric", params={"symbol": ticker, "metric": "all", **key},
                        timeout=15)
    metrics.raise_for_status()
    m = metrics.json().get("metric", {})
    cal = httpx.get(f"{base}/calendar/earnings", params={"symbol": ticker, **key}, timeout=15)
    next_date = ""
    if cal.status_code == 200:
        events = (cal.json().get("earningsCalendar") or [])
        if events:
            next_date = events[0].get("date", "")
    return {
        "ticker": ticker, "quarters": quarters, "next_earnings_date": next_date,
        "estimates": {"eps_next_q": None, "revenue_growth_pct": m.get("revenueGrowthTTMYoy")},
        "ratios": {
            "pe_forward": m.get("peTTM"), "ps": m.get("psTTM"),
            "debt_to_equity": m.get("totalDebt/totalEquityQuarterly"),
            "fcf_margin_pct": m.get("fcfMarginTTM"),
        },
    }


def _fmp_earnings(ticker: str) -> dict:
    base = "https://financialmodelingprep.com/api/v3"
    key = {"apikey": settings.fmp_api_key}
    surprises = httpx.get(f"{base}/earnings-surprises/{ticker}", params=key, timeout=15)
    surprises.raise_for_status()
    quarters = [
        {"period": s.get("date", ""), "eps_actual": s.get("actualEarningResult"),
         "eps_estimate": s.get("estimatedEarning"), "surprise_pct": None}
        for s in surprises.json()[:8]
    ]
    ratios = httpx.get(f"{base}/ratios-ttm/{ticker}", params=key, timeout=15)
    ratios.raise_for_status()
    r = (ratios.json() or [{}])[0]
    return {
        "ticker": ticker, "quarters": quarters, "next_earnings_date": "",
        "estimates": {"eps_next_q": None, "revenue_growth_pct": None},
        "ratios": {"pe_forward": r.get("peRatioTTM"), "ps": r.get("priceToSalesRatioTTM"),
                   "debt_to_equity": r.get("debtEquityRatioTTM"),
                   "fcf_margin_pct": None},
    }


def _finnhub_news(ticker: str, limit: int) -> list[dict]:
    from datetime import date, timedelta

    r = httpx.get(
        "https://finnhub.io/api/v1/company-news",
        params={"symbol": ticker,
                "from": (date.today() - timedelta(days=14)).isoformat(),
                "to": date.today().isoformat(),
                "token": settings.finnhub_api_key},
        timeout=15,
    )
    r.raise_for_status()
    return [
        {"headline": n.get("headline", ""), "date": n.get("datetime", ""),
         "source": n.get("source", ""), "url": n.get("url", ""),
         "provider": "finnhub"}
        for n in r.json()[:limit]
    ]


def _alpaca_news(ticker: str, limit: int) -> list[dict]:
    r = httpx.get(
        f"{settings.alpaca_data_base_url}/v1beta1/news",
        params={"symbols": ticker, "limit": limit, "sort": "desc"},
        headers={"APCA-API-KEY-ID": settings.alpaca_api_key,
                 "APCA-API-SECRET-KEY": settings.alpaca_api_secret},
        timeout=15,
    )
    r.raise_for_status()
    return [
        {"headline": n.get("headline", ""),
         "date": n.get("created_at", ""),
         "source": n.get("source", "benzinga"),
         "url": n.get("url", ""),
         "summary": (n.get("summary") or "")[:300],
         "provider": "alpaca"}
        for n in r.json().get("news") or []
    ]


def get_news(ticker: str, limit: int = 10) -> ToolResult:
    """Recent company news, merged from every configured provider
    (Finnhub company news + Alpaca/Benzinga real-time), deduplicated by
    headline. Sources are labeled so claims stay attributable."""
    ticker = ticker.upper()
    if settings.effective_demo_mode:
        return ToolResult(data=demo_data.NEWS["default"], source="demo")
    providers = []
    if settings.finnhub_api_key:
        providers.append(("finnhub", _finnhub_news))
    if settings.alpaca_api_key and settings.alpaca_api_secret:
        providers.append(("alpaca", _alpaca_news))
    if not providers:
        raise ToolError(
            "no news source configured (set FINNHUB_API_KEY and/or Alpaca "
            "keys); news claims must go in 'unverified'")
    merged: list[dict] = []
    seen: set[str] = set()
    used: list[str] = []
    errors: list[str] = []
    for name, fn in providers:
        try:
            items = fn(ticker, limit)
            used.append(name)
        except httpx.HTTPError as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        for n in items:
            key = (n.get("headline") or "").strip().lower()[:80]
            if key and key not in seen:
                seen.add(key)
                merged.append(n)
    if not used:
        raise ToolError(f"all news sources failed ({'; '.join(errors)}); "
                        "news claims must go in 'unverified'")
    merged.sort(key=lambda n: str(n.get("date", "")), reverse=True)
    return ToolResult(data=merged[: max(limit, 12)], source="+".join(used))
