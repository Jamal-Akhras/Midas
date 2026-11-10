"""Insider transactions carry DIRECTION. The old tool returned a bare list of
Form 4 URLs ("open URL for direction/size"); nobody ever opened one, and the
committee turned a count of filings into "active insider selling" (GOOGL,
2026-11-10). Finnhub's transaction codes give net bought vs sold; when that
source fails we fall back to the filing list and SAY it has no direction."""

from datetime import date, timedelta

import httpx
import pytest

from midas.config import settings
from midas.tools import edgar

TODAY = date.today()


def _row(name: str, code: str, change: int, derivative: bool = False) -> dict:
    return {
        "name": name,
        "transactionCode": code,
        "change": change,
        "share": 100_000 + change,
        "transactionPrice": 40.0,
        "transactionDate": (TODAY - timedelta(days=10)).isoformat(),
        "filingDate": (TODAY - timedelta(days=8)).isoformat(),
        "isDerivative": derivative,
        "symbol": "ACME",
    }


ROWS = [
    _row("CEO A", "P", 25_000),
    _row("CFO B", "M", 8_000),
    _row("CFO B", "S", -8_000),
    _row("CFO B", "F", -3_000),
    _row("VP C", "A", 5_000),
    _row("VP C", "G", -100),
    _row("VP C", "A", 1_000, derivative=True),  # RSU grant: not shares
]

SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["4", "10-Q", "4"],
            "filingDate": ["2026-11-05", "2026-10-30", "2026-10-29"],
            "accessionNumber": ["0001-26-000001", "0001-26-000002", "0001-26-000003"],
            "primaryDocument": ["form4.xml", "10q.htm", "form4.xml"],
        }
    }
}


@pytest.fixture()
def live(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setitem(edgar._cik_cache, "ACME", "0000000001")


def _fake_http(monkeypatch, *, finnhub_status: int = 200, rows: list[dict] | None = None):
    calls: list[tuple[str, dict]] = []

    def fake_get(url, *, params=None, headers=None, timeout=None):
        calls.append((url, dict(params or {})))
        req = httpx.Request("GET", url)
        if "finnhub.io" in url:
            if finnhub_status != 200:
                return httpx.Response(finnhub_status, json={"error": "down"}, request=req)
            return httpx.Response(200, json={"data": rows or [], "symbol": "ACME"}, request=req)
        if "data.sec.gov/submissions" in url:
            return httpx.Response(200, json=SUBMISSIONS, request=req)
        raise AssertionError(url)

    monkeypatch.setattr(edgar.httpx, "get", fake_get)
    return calls


def test_finnhub_codes_give_net_direction(live, monkeypatch):
    monkeypatch.setattr(settings, "finnhub_api_key", "fh-test")
    calls = _fake_http(monkeypatch, rows=ROWS)

    result = edgar.get_insider_transactions("ACME", limit=4)
    data = result.data

    assert result.source == "finnhub"
    assert data["direction_known"] is True
    assert data["window_days"] == 90
    assert data["counts_by_code"] == {"P": 1, "S": 1, "A": 1, "M": 1, "F": 1, "G": 1}
    assert data["open_market_bought_shares"] == 25_000
    assert data["open_market_sold_shares"] == 8_000
    assert data["net_open_market_shares"] == 17_000
    assert data["net_shares_all_codes"] == 25_000 + 8_000 - 8_000 - 3_000 + 5_000 - 100
    assert data["derivative_rows_excluded"] == 1
    assert len(data["rows"]) == 4  # limit honoured
    assert {"name", "transaction_code", "change", "price", "transaction_date"} <= set(
        data["rows"][0]
    )
    url, params = calls[0]
    assert "/stock/insider-transactions" in url
    assert params["symbol"] == "ACME"
    assert params["from"] == (TODAY - timedelta(days=90)).isoformat()
    assert params["to"] == TODAY.isoformat()


def test_quiet_window_is_still_direction_known(live, monkeypatch):
    monkeypatch.setattr(settings, "finnhub_api_key", "fh-test")
    _fake_http(monkeypatch, rows=[])
    data = edgar.get_insider_transactions("ACME").data
    assert data["direction_known"] is True
    assert data["counts_by_code"] == {}
    assert data["net_open_market_shares"] == 0


def test_finnhub_failure_falls_back_to_the_filing_list(live, monkeypatch):
    monkeypatch.setattr(settings, "finnhub_api_key", "fh-test")
    _fake_http(monkeypatch, finnhub_status=500)

    result = edgar.get_insider_transactions("ACME")
    data = result.data

    assert result.source == "edgar"
    assert data["direction_known"] is False
    assert "filing count only; not evidence of buying or selling" in data["note"]
    assert [f["form"] for f in data["filings"]] == ["4", "4"]
    assert all(
        f["url"].startswith("https://www.sec.gov/Archives/edgar/data/1/") for f in data["filings"]
    )


def test_no_finnhub_key_uses_the_filing_list_directly(live, monkeypatch):
    monkeypatch.setattr(settings, "finnhub_api_key", "")
    calls = _fake_http(monkeypatch)
    data = edgar.get_insider_transactions("ACME").data
    assert data["direction_known"] is False
    assert all("finnhub.io" not in u for u, _ in calls)
