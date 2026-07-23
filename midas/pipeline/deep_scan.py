"""Full-market deep scan: deterministic, $0 AI.

Every tradable US common stock -> bulk snapshots (price/liquidity floor) ->
daily bars for the most liquid survivors -> a setup score computed from
bar STRUCTURE (trend, extension, pullback depth, volume contraction), not
from today's green candle. The top names join the screener universe, where
the LLM funnel (screener -> triage -> committee) applies judgment.

Scale knobs are config: raise DEEP_SCAN_MAX_BARS and DEEP_SCAN_KEEP as the
account and committee budget grow - the architecture does not change.
"""
import logging
import time

import httpx

from midas.config import settings

logger = logging.getLogger(__name__)

_assets_cache: dict = {"date": "", "symbols": []}


def _headers() -> dict:
    return {"APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret}


def all_tradable_symbols() -> list[str]:
    """Every active, tradable US common-stock symbol (cached per day).
    ARCA is excluded on purpose: it is where most ETFs live."""
    from datetime import date

    today = date.today().isoformat()
    if _assets_cache["date"] == today and _assets_cache["symbols"]:
        return _assets_cache["symbols"]
    r = httpx.get(f"{settings.alpaca_paper_base_url}/v2/assets",
                  params={"status": "active", "asset_class": "us_equity"},
                  headers=_headers(), timeout=30)
    r.raise_for_status()
    _FUNDY = ("ETF", "ETN", " FUND", "TRUST", "INDEX", "PROSHARES",
              "ISHARES", "DIREXION", "VANGUARD", "SPDR")
    syms = [a["symbol"] for a in r.json()
            if a.get("tradable")
            and a.get("exchange") in ("NASDAQ", "NYSE", "AMEX")
            and a["symbol"].isalpha() and len(a["symbol"]) <= 5
            and not any(tag in (a.get("name") or "").upper()
                        for tag in _FUNDY)]
    _assets_cache.update(date=today, symbols=syms)
    return syms


def _snapshot_batch(symbols: list[str]) -> dict:
    for feed in ("sip", "iex"):
        r = httpx.get(f"{settings.alpaca_data_base_url}/v2/stocks/snapshots",
                      params={"symbols": ",".join(symbols), "feed": feed},
                      headers=_headers(), timeout=30)
        if r.status_code in (401, 403) and feed == "sip":
            continue
        r.raise_for_status()
        body = r.json()
        return body.get("snapshots", body) or {}
    return {}


def stage_a(snaps: dict) -> list[dict]:
    """Price + liquidity + sanity floors on snapshot data. Deterministic."""
    out = []
    for sym, s in snaps.items():
        day = s.get("dailyBar") or {}
        prev = s.get("prevDailyBar") or {}
        price = day.get("c") or (s.get("latestTrade") or {}).get("p")
        if not price or not day.get("v") or not prev.get("c"):
            continue
        if price < settings.deep_scan_min_price:
            continue
        dollar_vol = day["v"] * price
        if dollar_vol < settings.deep_scan_min_dollar_volume:
            continue
        chg = (price / prev["c"] - 1) * 100
        if not (-15 <= chg <= 25):  # halts, blowups, buyout gaps: not setups
            continue
        out.append({"ticker": sym, "price": round(float(price), 2),
                    "dollar_vol": dollar_vol, "day_chg_pct": round(chg, 2)})
    return out


def setup_score(bars: list[dict]) -> float | None:
    """Structure-based score from daily bars. None = no qualifying setup.
    Rewards: established uptrend, orderly 3-10% pullback from the 20d high,
    NOT extended above the 10d mean, contracting volume on the pullback."""
    if len(bars) < 40:
        return None
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    vols = [b["volume"] for b in bars]
    c = closes[-1]
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else sum(closes) / len(closes)
    if not (c > ma50 and ma20 > ma50):
        return None  # no uptrend, no setup
    extension = c / ma10 - 1
    if extension > 0.08 or extension < -0.05:
        return None  # chasing or breaking down
    high20 = max(highs[-20:])
    pullback = (1 - c / high20) * 100  # % below recent high
    if pullback > 12:
        return None  # that is not a pullback, that is a downtrend
    score = 60.0
    # sweet spot: 3-10% pullback (the entry IS the pullback)
    score += 25 - min(abs(pullback - 6.0) * 4, 25)
    # volume contraction while pulling back = supply drying up
    v5, v20 = sum(vols[-5:]) / 5, sum(vols[-20:]) / 20
    if v20 > 0 and v5 / v20 < 1.0:
        score += 10
    # gentle extension near zero is better than +7.9%
    score += max(0, 5 - abs(extension) * 100)
    return round(min(score, 100.0), 1)


def run_deep_scan(db) -> dict:
    """Sweep the whole market; persist survivors for today's screen."""
    from midas.models import SystemState
    from midas.models.base import utcnow
    from midas.tools.market_data import get_daily_bars

    today = utcnow().date().isoformat()
    row = db.get(SystemState, "deep_scan")
    if row and row.value.get("date") == today:
        return row.value  # once per day

    symbols = all_tradable_symbols()
    logger.info("deep scan: sweeping %s symbols (this takes a few minutes)",
                len(symbols))
    survivors: list[dict] = []
    for i in range(0, len(symbols), 100):
        try:
            survivors += stage_a(_snapshot_batch(symbols[i:i + 100]))
        except httpx.HTTPError as exc:
            logger.warning("deep scan batch %s failed: %s", i, type(exc).__name__)
        if i and i % 2000 == 0:
            logger.info("deep scan: %s/%s symbols, %s liquid so far",
                        i, len(symbols), len(survivors))
        time.sleep(0.35)  # stay far under the API rate limit

    survivors.sort(key=lambda x: -x["dollar_vol"])
    logger.info("deep scan: %s liquid survivors; bar analysis on top %s",
                len(survivors), settings.deep_scan_max_bars)
    checked, kept = 0, []
    for cand in survivors[: settings.deep_scan_max_bars]:
        try:
            bars = get_daily_bars(cand["ticker"], days=60).data
        except Exception:
            continue
        checked += 1
        score = setup_score(bars)
        if score is not None:
            kept.append({"ticker": cand["ticker"], "score": score,
                         "price": cand["price"]})
    kept.sort(key=lambda x: -x["score"])
    kept = kept[: settings.deep_scan_keep]

    value = {"date": today, "symbols_scanned": len(symbols),
             "stage_a_survivors": len(survivors), "bars_checked": checked,
             "kept": kept}
    if row is None:
        db.add(SystemState(key="deep_scan", value=value))
    else:
        row.value = value
    db.flush()
    logger.info("deep scan: %s symbols -> %s liquid -> %s with setups",
                len(symbols), len(survivors), len(kept))
    return value
