"""Canned tool data for demo mode: develop the full pipeline with zero
API/data calls. Fixtures are realistic in SHAPE (same keys the real tools
return) but the values are synthetic - clearly labeled source='demo'.
"""
from datetime import date, timedelta
from typing import Any


def _iso(d: date) -> str:
    return d.isoformat()


TODAY = date.today()

DEMO_UNIVERSE = ["ACME", "BOLT", "CRUX", "DUNE", "EMBR"]

QUOTES: dict[str, dict[str, Any]] = {
    "ACME": {"price": 184.20, "prev_close": 181.10, "avg_volume": 8_400_000, "sector": "Technology"},
    "BOLT": {"price": 62.35, "prev_close": 63.90, "avg_volume": 3_100_000, "sector": "Industrials"},
    "CRUX": {"price": 12.05, "prev_close": 12.10, "avg_volume": 650_000, "sector": "Energy"},
    "DUNE": {"price": 91.80, "prev_close": 88.4, "avg_volume": 5_600_000, "sector": "Healthcare"},
    "EMBR": {"price": 3.15, "prev_close": 3.22, "avg_volume": 12_000_000, "sector": "Technology"},
}


def daily_bars(ticker: str, days: int = 60) -> list[dict]:
    q = QUOTES.get(ticker, {"price": 50.0})
    base = q["price"] * 0.9
    bars = []
    for i in range(days):
        d = TODAY - timedelta(days=days - i)
        drift = base * (1 + 0.002 * i)
        bars.append({
            "date": _iso(d), "open": round(drift * 0.995, 2),
            "high": round(drift * 1.012, 2), "low": round(drift * 0.988, 2),
            "close": round(drift, 2),
            "volume": int(q.get("avg_volume", 1_000_000) * (0.8 + 0.05 * (i % 8))),
        })
    return bars


def options_chain(ticker: str) -> list[dict]:
    q = QUOTES.get(ticker, {"price": 50.0})
    spot = q["price"]
    chain = []
    for dte in (10, 21, 35, 49, 63, 91):
        expiry = _iso(TODAY + timedelta(days=dte))
        for pct in (-0.10, -0.05, 0.0, 0.05, 0.10):
            strike = round(spot * (1 + pct), 0)
            for opt_type in ("call", "put"):
                itm = (opt_type == "call" and strike < spot) or (opt_type == "put" and strike > spot)
                intrinsic = abs(spot - strike) if itm else 0.0
                time_val = spot * 0.018 * (dte / 30) ** 0.5
                mid = round(intrinsic + time_val, 2)
                spread = 0.04 if abs(pct) <= 0.05 else 0.12
                chain.append({
                    "expiry": expiry, "dte": dte, "type": opt_type,
                    "strike": strike,
                    "bid": round(mid * (1 - spread / 2), 2),
                    "ask": round(mid * (1 + spread / 2), 2),
                    "mid": mid,
                    "open_interest": 2400 if abs(pct) <= 0.05 else 150,
                    "volume": 380 if abs(pct) <= 0.05 else 12,
                    "iv": round(0.32 + abs(pct), 3),
                    "delta": round((0.5 - pct * 4) if opt_type == "call" else (-0.5 - pct * 4), 2),
                })
    return chain


EDGAR_FILINGS = {
    "default": [
        {"form": "10-K", "filed": _iso(TODAY - timedelta(days=90)),
         "url": "https://www.sec.gov/Archives/edgar/data/0000000000/demo-10k.htm",
         "summary": "Annual report. Risk factors: supply concentration (2 vendors), FX exposure, customer concentration (top 3 = 41% revenue). Segments: Core Platform 71%, Services 29%."},
        {"form": "10-Q", "filed": _iso(TODAY - timedelta(days=20)),
         "url": "https://www.sec.gov/Archives/edgar/data/0000000000/demo-10q.htm",
         "summary": "Quarterly report. Revenue +18% YoY, gross margin 61% (up 140bps), deferred revenue +22%. New $200M buyback authorized."},
        {"form": "8-K", "filed": _iso(TODAY - timedelta(days=12)),
         "url": "https://www.sec.gov/Archives/edgar/data/0000000000/demo-8k.htm",
         "summary": "Material event: multi-year platform partnership with a top-5 cloud provider; CFO transition announced (retirement, 6-month handover)."},
        {"form": "DEF 14A", "filed": _iso(TODAY - timedelta(days=150)),
         "url": "https://www.sec.gov/Archives/edgar/data/0000000000/demo-proxy.htm",
         "summary": "Proxy: CEO tenure 6 years, founder-led board (2 of 9 independent flagged by ISS), executive comp 70% performance-based (rTSR + revenue growth)."},
    ]
}

INSIDER_TRANSACTIONS = {
    "default": [
        {"insider": "CEO J. Marsh", "type": "buy", "shares": 25000,
         "date": _iso(TODAY - timedelta(days=18)), "value_usd": 4_600_000,
         "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4&demo=1"},
        {"insider": "CFO L. Ortiz", "type": "sell", "shares": 8000,
         "date": _iso(TODAY - timedelta(days=45)), "value_usd": 1_450_000,
         "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4&demo=2"},
    ]
}

EARNINGS = {
    "default": {
        "next_earnings_date": _iso(TODAY + timedelta(days=32)),
        "quarters": [
            {"period": "2026Q1", "eps_actual": 1.42, "eps_estimate": 1.31, "surprise_pct": 8.4},
            {"period": "2025Q4", "eps_actual": 1.28, "eps_estimate": 1.25, "surprise_pct": 2.4},
            {"period": "2025Q3", "eps_actual": 1.11, "eps_estimate": 1.14, "surprise_pct": -2.6},
            {"period": "2025Q2", "eps_actual": 1.05, "eps_estimate": 0.98, "surprise_pct": 7.1},
        ],
        "estimates": {"eps_next_q": 1.51, "revenue_growth_pct": 16.5},
        "ratios": {"pe_forward": 24.3, "ps": 6.1, "debt_to_equity": 0.38, "fcf_margin_pct": 22.0},
    }
}

NEWS = {
    "default": [
        {"headline": "Company expands cloud partnership, analysts raise targets",
         "date": _iso(TODAY - timedelta(days=3)), "source": "demo-wire",
         "url": "https://news.example.com/demo/cloud-partnership"},
        {"headline": "Sector rotation: institutional flows favor quality growth",
         "date": _iso(TODAY - timedelta(days=6)), "source": "demo-wire",
         "url": "https://news.example.com/demo/sector-rotation"},
    ]
}

WEB_RESULTS = {
    "default": [
        {"title": "Deep dive: platform moat and pricing power",
         "url": "https://research.example.com/demo/deep-dive",
         "snippet": "Net revenue retention 118%; pricing raised 8% with churn under 1.5%."},
        {"title": "Management track record analysis",
         "url": "https://research.example.com/demo/management",
         "snippet": "CEO previously scaled two infra businesses; CFO transition flagged as watch item."},
    ]
}
