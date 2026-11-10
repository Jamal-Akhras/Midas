"""Full-market deep scan: deterministic, $0 AI.

Every tradable US common stock -> bulk snapshots on the delayed consolidated
feed (price floor, PREVIOUS-day liquidity floor) -> daily bars for the most
liquid survivors -> a setup score computed from the STRUCTURE of completed
sessions (trend, extension, pullback depth, volume contraction), not from
today's green candle. The top names join the screener universe, where the
LLM funnel (screener -> triage -> committee) applies judgment.

Scale knobs are config: raise DEEP_SCAN_MAX_BARS and DEEP_SCAN_KEEP as the
account and committee budget grow - the architecture does not change.
"""

import logging
import time
from datetime import datetime, timezone

import httpx

from midas.config import settings

logger = logging.getLogger(__name__)

_assets_cache: dict = {"date": "", "symbols": []}


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }


def all_tradable_symbols() -> list[str]:
    """Every active, tradable US common-stock symbol (cached per day).
    ARCA is excluded on purpose: it is where most ETFs live."""
    from datetime import date

    today = date.today().isoformat()
    if _assets_cache["date"] == today and _assets_cache["symbols"]:
        return _assets_cache["symbols"]
    r = httpx.get(
        f"{settings.alpaca_paper_base_url}/v2/assets",
        params={"status": "active", "asset_class": "us_equity"},
        headers=_headers(),
        timeout=30,
    )
    r.raise_for_status()
    _FUNDY = (
        "ETF",
        "ETN",
        " FUND",
        "TRUST",
        "INDEX",
        "PROSHARES",
        "ISHARES",
        "DIREXION",
        "VANGUARD",
        "SPDR",
    )
    syms = [
        a["symbol"]
        for a in r.json()
        if a.get("tradable")
        and a.get("exchange") in ("NASDAQ", "NYSE", "AMEX")
        and a["symbol"].isalpha()
        and len(a["symbol"]) <= 5
        and not any(tag in (a.get("name") or "").upper() for tag in _FUNDY)
    ]
    _assets_cache.update(date=today, symbols=syms)
    return syms


# Snapshot feeds in order of preference. `delayed_sip` is the consolidated
# tape 15 minutes behind: the only consolidated view the free plan allows on
# this endpoint (`sip` answers 403 and is not asked for - that was a wasted
# call per batch). `iex` is real-time but carries 3-10% of the volume, and a
# name that has not printed there yet still shows YESTERDAY's bar as today's;
# it is the fallback, and the feed used is persisted with the scan.
_SNAPSHOT_FEEDS = ("delayed_sip", "iex")


def _snapshot_batch(symbols: list[str]) -> tuple[dict, str]:
    """Bulk snapshots for up to 100 symbols and the feed that served them.
    Raises httpx.HTTPError when the last feed is refused too: that is keys
    or plan, not an empty market, and the caller logs the batch."""
    for feed in _SNAPSHOT_FEEDS:
        r = httpx.get(
            f"{settings.alpaca_data_base_url}/v2/stocks/snapshots",
            params={"symbols": ",".join(symbols), "feed": feed},
            headers=_headers(),
            timeout=30,
        )
        if r.status_code in (401, 403) and feed != _SNAPSHOT_FEEDS[-1]:
            continue
        r.raise_for_status()
        body = r.json()
        return body.get("snapshots", body) or {}, feed
    return {}, ""  # unreachable: the last feed raises; keeps the signature total


def stage_a(snaps: dict) -> list[dict]:
    """Price + liquidity + sanity floors on snapshot data. Deterministic.

    Liquidity is the PREVIOUS full session's dollar volume: at 09:45 today's
    bar holds a few minutes of trading, and on IEX a name that has not
    printed yet still carries yesterday's bar as today's (RSG, 2026-11-10).
    Price is the latest trade, so the day-change filter sees today's gap even
    when the day bar is stale. A snapshot without previous-day volume falls
    back to the day bar (fixtures; Alpaca always sends it)."""
    out = []
    for sym, s in snaps.items():
        day = s.get("dailyBar") or {}
        prev = s.get("prevDailyBar") or {}
        trade = s.get("latestTrade") or {}
        price = trade.get("p") or day.get("c") or prev.get("c")
        if not price or not prev.get("c"):
            continue
        if price < settings.deep_scan_min_price:
            continue
        if prev.get("v"):
            prev_dollar_vol = prev["v"] * prev["c"]
        elif day.get("v"):
            prev_dollar_vol = day["v"] * price
        else:
            continue
        if prev_dollar_vol < settings.deep_scan_min_dollar_volume:
            continue
        chg = (price / prev["c"] - 1) * 100
        if not (-15 <= chg <= 25):  # halts, blowups, buyout gaps: not setups
            continue
        out.append(
            {
                "ticker": sym,
                "price": round(float(price), 2),
                "dollar_vol": (day.get("v") or 0) * price,  # today so far, for reference
                "prev_dollar_vol": prev_dollar_vol,
                "day_chg_pct": round(chg, 2),
            }
        )
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


def completed_bars(bars: list[dict], today: str) -> list[dict]:
    """Drop a trailing bar dated `today`: during the session Alpaca serves the
    day in progress as a daily bar, and at 09:45 it holds the opening print
    as its close and 2-20% of a normal day's volume. Scored as a full session
    it drags the five-day volume average down, so the contraction bonus fires
    on volume that simply has not traded yet (RSG, 2026-11-10: v5/v20 1.12 on
    completed bars, 0.95 with the partial one: +10 points). Pure; `today` is
    the exchange date, see _today_new_york."""
    if bars and str(bars[-1].get("date", "")) == today:
        return bars[:-1]
    return bars


def _today_new_york(now: datetime | None = None) -> str:
    """The exchange's calendar date (Alpaca stamps daily bars in New York
    time); UTC is already tomorrow from 19:00/20:00 ET. Falls back to the UTC
    date only when the zone database is missing."""
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return now.date().isoformat()


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
    logger.info("deep scan: sweeping %s symbols (this takes a few minutes)", len(symbols))
    survivors: list[dict] = []
    feeds: set[str] = set()
    for i in range(0, len(symbols), 100):
        try:
            snaps, feed = _snapshot_batch(symbols[i : i + 100])
            feeds.add(feed)
            survivors += stage_a(snaps)
        except httpx.HTTPError as exc:
            logger.warning("deep scan batch %s failed: %s", i, type(exc).__name__)
        if i and i % 2000 == 0:
            logger.info(
                "deep scan: %s/%s symbols, %s liquid so far", i, len(symbols), len(survivors)
            )
        time.sleep(0.35)  # stay far under the API rate limit

    # rank by the previous full session: today's partial bar would put the
    # morning's news names first and the rest in order of minutes traded
    survivors.sort(key=lambda x: -x["prev_dollar_vol"])
    logger.info(
        "deep scan: %s liquid survivors; bar analysis on top %s",
        len(survivors),
        settings.deep_scan_max_bars,
    )
    session_date = _today_new_york()
    checked, kept = 0, []
    for cand in survivors[: settings.deep_scan_max_bars]:
        try:
            bars = completed_bars(get_daily_bars(cand["ticker"], days=60).data, session_date)
        except Exception:
            continue
        checked += 1
        score = setup_score(bars)
        if score is not None:
            kept.append({"ticker": cand["ticker"], "score": score, "price": cand["price"]})
    kept.sort(key=lambda x: -x["score"])
    kept = kept[: settings.deep_scan_keep]

    value = {
        "date": today,
        "feed": "+".join(sorted(feeds)),  # the tape the floors were applied on
        "symbols_scanned": len(symbols),
        "stage_a_survivors": len(survivors),
        "bars_checked": checked,
        "kept": kept,
    }
    if row is None:
        db.add(SystemState(key="deep_scan", value=value))
    else:
        row.value = value
    db.flush()
    logger.info(
        "deep scan: %s symbols -> %s liquid -> %s with setups",
        len(symbols),
        len(survivors),
        len(kept),
    )
    return value
