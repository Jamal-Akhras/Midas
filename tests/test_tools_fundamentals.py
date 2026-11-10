"""The fundamentals tool labels its fields honestly. Finnhub's metric
endpoint serves TRAILING ratios (peTTM, psTTM, revenueGrowthTTMYoy); the
committee once read them as "forward P/E" and "projected revenue growth"
and refused mega-caps on that basis. The earnings calendar must return the
NEXT report date: queried without a window, Finnhub answers with the most
recent PAST one, which the committee then planned around (2026-11-10)."""

import json
from datetime import date, timedelta

import httpx
import pytest

from midas.config import settings
from midas.tools import fundamentals_news as fn

TODAY = date.today()


def _iso(d: date) -> str:
    return d.isoformat()


@pytest.fixture()
def live_finnhub(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")  # live mode on
    monkeypatch.setattr(settings, "finnhub_api_key", "fh-test")
    monkeypatch.setattr(settings, "fmp_api_key", "")


def _fake_finnhub(monkeypatch, calendar_events: list[dict]) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def fake_get(url, *, params=None, timeout=None, headers=None):
        calls.append((url, dict(params or {})))
        req = httpx.Request("GET", url)
        if url.endswith("/stock/earnings"):
            body = [
                {"period": "2026-09-30", "actual": 2.1, "estimate": 2.0263, "surprisePercent": 3.64}
            ]
            return httpx.Response(200, json=body, request=req)
        if url.endswith("/stock/metric"):
            metric = {
                "peTTM": 38.3713,
                "psTTM": 10.5918,
                "revenueGrowthTTMYoy": 15.06,
                "totalDebt/totalEquityQuarterly": 0.7147,
                "fcfMarginTTM": 24.5,
            }
            return httpx.Response(200, json={"metric": metric}, request=req)
        if url.endswith("/calendar/earnings"):
            return httpx.Response(200, json={"earningsCalendar": calendar_events}, request=req)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(fn.httpx, "get", fake_get)
    return calls


def test_trailing_ratios_are_labelled_trailing(live_finnhub, monkeypatch):
    _fake_finnhub(monkeypatch, [])
    data = fn.get_earnings("AAPL").data
    assert data["ratios"]["pe_ttm"] == 38.3713
    assert data["ratios"]["ps_ttm"] == 10.5918
    assert data["revenue_growth_ttm_yoy_pct"] == 15.06
    # no KEY may call trailing data forward or projected, and the old
    # "estimates" block (TTM growth posing as a projection) is gone; the only
    # forward-looking key is the explicit "unavailable" marker. A quarter's
    # eps_estimate (consensus vs actual for a REPORTED quarter) is honest.
    keys = _all_keys(data) - {"forward_estimates"}
    for forbidden in ("forward", "projected"):
        assert not [k for k in keys if forbidden in k.lower()], (forbidden, keys)
    assert "estimates" not in data
    assert data["forward_estimates"]["available"] is False


def _all_keys(obj) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _all_keys(v)
    return out


def test_next_earnings_date_is_the_first_future_one(live_finnhub, monkeypatch):
    nxt = TODAY + timedelta(days=78)
    calls = _fake_finnhub(
        monkeypatch,
        [
            {"date": _iso(TODAY - timedelta(days=8)), "quarter": 4},  # last report (past)
            {"date": _iso(nxt + timedelta(days=91)), "quarter": 2},
            {"date": _iso(nxt), "quarter": 1},
        ],
    )
    data = fn.get_earnings("AAPL").data
    assert data["next_earnings_date"] == _iso(nxt)
    cal = [p for u, p in calls if u.endswith("/calendar/earnings")]
    assert cal and cal[0]["from"] == _iso(TODAY)
    assert cal[0]["to"] == _iso(TODAY + timedelta(days=120))


def test_next_earnings_date_empty_when_nothing_is_scheduled(live_finnhub, monkeypatch):
    _fake_finnhub(monkeypatch, [{"date": _iso(TODAY - timedelta(days=8))}])
    assert fn.get_earnings("AAPL").data["next_earnings_date"] == ""


def test_fmp_branch_uses_the_same_honest_labels(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "finnhub_api_key", "")
    monkeypatch.setattr(settings, "fmp_api_key", "fmp-test")

    def fake_get(url, *, params=None, timeout=None, headers=None):
        req = httpx.Request("GET", url)
        if "/earnings-surprises/" in url:
            body = [{"date": "2026-09-30", "actualEarningResult": 2.1, "estimatedEarning": 2.0}]
            return httpx.Response(200, json=body, request=req)
        if "/ratios-ttm/" in url:
            body = [{"peRatioTTM": 30.5, "priceToSalesRatioTTM": 8.1, "debtEquityRatioTTM": 0.5}]
            return httpx.Response(200, json=body, request=req)
        raise AssertionError(url)

    monkeypatch.setattr(fn.httpx, "get", fake_get)
    data = fn.get_earnings("ORCL").data
    assert data["ratios"]["pe_ttm"] == 30.5 and data["ratios"]["ps_ttm"] == 8.1
    assert "pe_forward" not in json.dumps(data)
    assert data["forward_estimates"]["available"] is False
