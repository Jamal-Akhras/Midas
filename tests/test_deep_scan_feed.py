"""Deep-scan discovery after the 2026-11-10 review.

Alpaca's free plan answers `feed=sip` on the snapshot endpoint with HTTP 403,
so the sweep silently ran on IEX, where volume is 3-10% of the consolidated
tape and a name that has not printed on IEX yet still carries YESTERDAY's bar
as today's. A $20M floor on today's partial IEX bar left ~15 mega-caps out of
6,400 symbols, and the structure score read the day in progress as a full
session. These tests pin the corrected behaviour: delayed consolidated feed
first, liquidity floor on the previous full day, structure on completed bars.
"""

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from midas.config import settings
from midas.pipeline import deep_scan
from midas.tools.base import ToolResult


def _snap(prev_v, prev_c, day_v=None, day_c=None, trade=None) -> dict:
    s: dict = {"prevDailyBar": {"c": prev_c, "v": prev_v}}
    if day_c is not None:
        s["dailyBar"] = {"c": day_c, "v": day_v}
    if trade is not None:
        s["latestTrade"] = {"p": trade, "t": "2026-11-10T14:30:00Z"}
    return s


def _fake_snapshots(monkeypatch, served: dict[str, dict]) -> list[dict]:
    """`served[feed]` is the snapshot body for that feed; any other feed is
    refused with 403 the way the free plan refuses `sip`."""
    calls: list[dict] = []

    def fake_get(url, *, params=None, headers=None, timeout=None):
        calls.append(dict(params))
        req = httpx.Request("GET", url)
        feed = params["feed"]
        if feed not in served:
            return httpx.Response(
                403,
                json={"message": "subscription does not permit querying recent SIP data"},
                request=req,
            )
        return httpx.Response(200, json={"snapshots": served[feed]}, request=req)

    monkeypatch.setattr(settings, "alpaca_api_key", "k")
    monkeypatch.setattr(settings, "alpaca_api_secret", "s")
    monkeypatch.setattr(deep_scan.httpx, "get", fake_get)
    return calls


def _floors(monkeypatch) -> None:
    monkeypatch.setattr(settings, "deep_scan_min_price", 5.0)
    monkeypatch.setattr(settings, "deep_scan_min_dollar_volume", 20_000_000.0)


# ---------------------------------------------------------------- S1: feed


def test_snapshots_use_the_delayed_consolidated_feed_first(monkeypatch):
    calls = _fake_snapshots(
        monkeypatch,
        {
            "delayed_sip": {"RSG": _snap(1_691_418, 246.78)},
            "iex": {"RSG": _snap(91_937, 246.81)},
        },
    )

    snaps, feed = deep_scan._snapshot_batch(["RSG"])

    assert feed == "delayed_sip"
    assert snaps["RSG"]["prevDailyBar"]["v"] == 1_691_418  # consolidated, not IEX's 5%
    # `sip` is 403 on this plan: asking for it is a wasted call per batch
    assert [c["feed"] for c in calls] == ["delayed_sip"]


def test_snapshots_fall_back_to_iex_when_the_delayed_feed_is_denied(monkeypatch):
    calls = _fake_snapshots(monkeypatch, {"iex": {"RSG": _snap(91_937, 246.81)}})

    snaps, feed = deep_scan._snapshot_batch(["RSG"])

    assert feed == "iex" and snaps["RSG"]["prevDailyBar"]["v"] == 91_937
    assert [c["feed"] for c in calls] == ["delayed_sip", "iex"]


def test_snapshot_batch_raises_when_every_feed_is_denied(monkeypatch):
    """Both feeds refused means keys or plan, not an empty market: the batch
    must fail loudly (run_deep_scan logs it) rather than report no survivors."""
    _fake_snapshots(monkeypatch, {})
    with pytest.raises(httpx.HTTPError):
        deep_scan._snapshot_batch(["RSG"])


def test_snapshot_batch_raises_on_other_http_errors(monkeypatch):
    """A 500 must surface as httpx.HTTPError: run_deep_scan skips that batch
    and logs it, instead of treating an outage as an empty market."""

    def fake_get(url, *, params=None, headers=None, timeout=None):
        return httpx.Response(500, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(deep_scan.httpx, "get", fake_get)
    with pytest.raises(httpx.HTTPError):
        deep_scan._snapshot_batch(["RSG"])


# ------------------------------------------------------------- S2: stage A


def test_stage_a_floors_on_the_previous_full_day_not_todays_partial_bar(monkeypatch):
    _floors(monkeypatch)
    snaps = {
        # 09:45: 1.6% of a normal day traded so far ($6.9M); yesterday was $417M
        "RSG": _snap(1_691_418, 246.78, day_v=27_879, day_c=245.97, trade=246.65),
        # thin every day: yesterday $5M
        "THIN": _snap(100_000, 50.0, day_v=50_000, day_c=50.0, trade=50.0),
        # a news spike: $30M already today, but only $8M yesterday
        "NEWS": _snap(400_000, 20.0, day_v=1_500_000, day_c=20.0, trade=20.0),
    }

    out = deep_scan.stage_a(snaps)

    assert [x["ticker"] for x in out] == ["RSG"]
    rsg = out[0]
    assert rsg["prev_dollar_vol"] == pytest.approx(1_691_418 * 246.78)
    assert rsg["price"] == 246.65  # the latest trade, not a bar close
    assert rsg["day_chg_pct"] == pytest.approx((246.65 / 246.78 - 1) * 100, abs=0.01)
    assert rsg["dollar_vol"] == pytest.approx(27_879 * 246.65)  # today so far, kept for reference


def test_stage_a_price_falls_back_from_trade_to_day_bar_to_previous_close(monkeypatch):
    _floors(monkeypatch)
    snaps = {
        "TRADE": _snap(1_000_000, 100.0, day_v=10_000, day_c=101.0, trade=102.0),
        "DAYBAR": _snap(1_000_000, 100.0, day_v=10_000, day_c=101.0),
        "PREV": _snap(1_000_000, 100.0),
    }

    out = {x["ticker"]: x for x in deep_scan.stage_a(snaps)}

    assert out["TRADE"]["price"] == 102.0
    assert out["DAYBAR"]["price"] == 101.0
    assert out["PREV"]["price"] == 100.0 and out["PREV"]["day_chg_pct"] == 0.0


def test_stage_a_day_change_is_the_latest_trade_against_the_previous_close(monkeypatch):
    """On IEX a name that has not printed today still shows yesterday's bar as
    `dailyBar`; a close-vs-close change would then compare the wrong two days
    and wave a -20% gapper through. The trade carries today's gap."""
    _floors(monkeypatch)
    snaps = {
        "GAPDN": _snap(1_000_000, 100.0, day_v=1_000_000, day_c=100.0, trade=84.0),  # -16%
        "GAPUP": _snap(1_000_000, 100.0, day_v=1_000_000, day_c=100.0, trade=124.0),  # +24%
        "BLOWUP": _snap(1_000_000, 100.0, day_v=1_000_000, day_c=100.0, trade=126.0),  # +26%
    }

    out = [x["ticker"] for x in deep_scan.stage_a(snaps)]

    assert out == ["GAPUP"]


def test_stage_a_keeps_the_price_floor(monkeypatch):
    _floors(monkeypatch)
    snaps = {"PENNY": _snap(50_000_000, 2.0, day_v=1_000_000, day_c=2.0, trade=2.0)}
    assert deep_scan.stage_a(snaps) == []


def test_stage_a_without_previous_day_volume_falls_back_to_the_day_bar(monkeypatch):
    """Alpaca always sends prevDailyBar.v; fixtures (tests/test_evidence.py)
    do not. The fallback keeps those honest instead of silently empty."""
    _floors(monkeypatch)
    snaps = {
        "GOOD": {"dailyBar": {"c": 50.0, "v": 1_000_000}, "prevDailyBar": {"c": 49.0}},
        "ILLIQ": {"dailyBar": {"c": 50.0, "v": 10_000}, "prevDailyBar": {"c": 50.0}},
        "NOVOL": {"dailyBar": {"c": 50.0}, "prevDailyBar": {"c": 50.0}},
    }
    assert [x["ticker"] for x in deep_scan.stage_a(snaps)] == ["GOOD"]


# ------------------------------------------------- S3: completed bars only


def test_completed_bars_drops_todays_partial_bar_only():
    bars = [{"date": "2026-11-06"}, {"date": "2026-11-09"}, {"date": "2026-11-10"}]
    assert deep_scan.completed_bars(bars, "2026-11-10") == bars[:2]
    # pre-open or a weekend: the last bar is a finished session, keep it
    assert deep_scan.completed_bars(bars[:2], "2026-11-10") == bars[:2]
    assert deep_scan.completed_bars([], "2026-11-10") == []


def test_today_new_york_is_the_exchange_date_not_utc():
    # 02:30 UTC on the 11th is still the evening of the 10th in New York
    late = datetime(2026, 11, 11, 2, 30, tzinfo=timezone.utc)
    assert deep_scan._today_new_york(late) == "2026-11-10"
    noon = datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc)
    assert deep_scan._today_new_york(noon) == "2026-11-10"


def _sessions(n: int, last: str) -> list[str]:
    """`n` weekday dates ending on `last`, oldest first."""
    day = date.fromisoformat(last)
    out: list[str] = []
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day -= timedelta(days=1)
    return out[::-1]


def _setup_bars(partial_volume: int | None) -> list[dict]:
    """55 completed sessions: a steady uptrend then an orderly 4% pullback on
    volume that is NOT contracting (last five sessions 1.2x the mean). With a
    partial bar for today appended, the five-day average collapses and the
    contraction bonus fires on volume that simply has not traded yet."""
    closes = [100 + i * 0.6 for i in range(50)] + [129.0, 128.0, 127.0, 126.0, 125.0]
    vols = [1_000_000] * 50 + [1_200_000] * 5
    bars = [
        {"date": day, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": v}
        for day, c, v in zip(_sessions(len(closes), "2026-11-09"), closes, vols, strict=True)
    ]
    if partial_volume is not None:
        bars.append(
            {
                "date": "2026-11-10",
                "open": 125.0,
                "high": 125.4,
                "low": 124.6,
                "close": 124.8,
                "volume": partial_volume,
            }
        )
    return bars


def test_partial_bar_fixture_flips_the_contraction_bonus():
    """Sanity check on the fixture: the two scores must differ for the
    run_deep_scan test below to prove anything."""
    completed = deep_scan.setup_score(_setup_bars(None))
    with_partial = deep_scan.setup_score(_setup_bars(20_000))
    assert completed is not None and with_partial is not None
    assert with_partial > completed


def _run_scan(db, monkeypatch, snaps: dict, bars_by_ticker: dict, feed: str = "delayed_sip"):
    monkeypatch.setattr(deep_scan, "all_tradable_symbols", lambda: list(snaps))
    monkeypatch.setattr(deep_scan, "_snapshot_batch", lambda syms: (snaps, feed))
    monkeypatch.setattr(deep_scan.time, "sleep", lambda s: None)
    monkeypatch.setattr(deep_scan, "_today_new_york", lambda now=None: "2026-11-10")
    fetched: list[str] = []

    def fake_bars(ticker, days=60):
        fetched.append(ticker)
        return ToolResult(data=bars_by_ticker[ticker], source="alpaca")

    monkeypatch.setattr("midas.tools.market_data.get_daily_bars", fake_bars)
    return deep_scan.run_deep_scan(db), fetched


def test_run_deep_scan_scores_completed_bars_and_records_the_feed(db, monkeypatch):
    _floors(monkeypatch)
    snaps = {"SETUP": _snap(5_000_000, 125.0, day_v=20_000, day_c=124.8, trade=124.8)}

    value, _ = _run_scan(db, monkeypatch, snaps, {"SETUP": _setup_bars(20_000)})

    assert value["feed"] == "delayed_sip"
    assert [k["ticker"] for k in value["kept"]] == ["SETUP"]
    assert value["kept"][0]["score"] == deep_scan.setup_score(_setup_bars(None))


def test_run_deep_scan_ranks_survivors_by_prior_day_dollar_volume(db, monkeypatch):
    """Only the top `deep_scan_max_bars` survivors get bar analysis, so the
    ranking decides which names the scan ever looks at. Today's partial bar
    would put the morning's news name first; the previous full day is the
    liquidity the floor is about."""
    _floors(monkeypatch)
    monkeypatch.setattr(settings, "deep_scan_max_bars", 1)
    snaps = {
        "SMALL": _snap(1_000_000, 30.0, day_v=800_000, day_c=30.0, trade=30.0),  # $30M / $24M
        "BIG": _snap(10_000_000, 30.0, day_v=100_000, day_c=30.0, trade=30.0),  # $300M / $3M
    }
    bars = {"BIG": _setup_bars(None), "SMALL": _setup_bars(None)}

    value, fetched = _run_scan(db, monkeypatch, snaps, bars)

    assert fetched == ["BIG"]
    assert value["stage_a_survivors"] == 2 and value["bars_checked"] == 1
