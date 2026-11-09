"""Alpaca daily bars must be the NEWEST `days` bars. Alpaca pages oldest-first
and caps each page at `limit`, so a naive `limit=days` request over a wide
window stops weeks before today; the trend filter and the quote's previous
close would then read a stale window without anyone noticing."""

import httpx

from midas.config import settings
from midas.tools import market_data


def _bar(day: str, close: float) -> dict:
    return {"t": f"{day}T05:00:00Z", "o": close, "h": close, "l": close, "c": close, "v": 1000}


def _fake_alpaca(monkeypatch, pages: dict[str, list[tuple[list[dict], str | None]]]):
    """`pages[feed]` is the sequence of (bars, next_page_token) replies for that
    feed; a feed with no entry is denied with 403."""
    calls: list[dict] = []
    served: dict[str, int] = {}

    def fake_get(url, *, params=None, headers=None, timeout=None):
        calls.append(dict(params))
        req = httpx.Request("GET", url)
        feed = params["feed"]
        if feed not in pages:
            return httpx.Response(403, json={"message": "subscription"}, request=req)
        bars, token = pages[feed][served.get(feed, 0)]
        served[feed] = served.get(feed, 0) + 1
        return httpx.Response(200, json={"bars": bars, "next_page_token": token}, request=req)

    monkeypatch.setattr(settings, "alpaca_api_key", "k")
    monkeypatch.setattr(settings, "alpaca_api_secret", "s")
    monkeypatch.setattr(market_data.httpx, "get", fake_get)
    return calls


def test_bars_follow_pagination_and_keep_the_newest_days(monkeypatch):
    old = [_bar(f"2026-09-{d:02d}", 100 + d) for d in range(1, 11)]
    new = [_bar(f"2026-10-{d:02d}", 200 + d) for d in range(1, 6)]
    calls = _fake_alpaca(monkeypatch, {"sip": [(old, "tok1"), (new, None)]})

    bars = market_data._alpaca_bars("QQQ", days=7)

    assert [b["date"] for b in bars] == ["2026-09-09", "2026-09-10"] + [
        f"2026-10-0{d}" for d in range(1, 6)
    ]
    assert bars[-1]["close"] == 205  # the newest bar, not the oldest
    first, second = calls
    assert first["limit"] >= 1000 and "page_token" not in first
    assert "end" in first and first["start"] < first["end"]
    assert second["page_token"] == "tok1"


def test_sip_denied_falls_back_to_iex(monkeypatch):
    new = [_bar(f"2026-10-{d:02d}", 200 + d) for d in range(1, 6)]
    calls = _fake_alpaca(monkeypatch, {"iex": [(new, None)]})

    bars = market_data._alpaca_bars("SPY", days=3)

    assert [c["feed"] for c in calls] == ["sip", "iex"]
    assert [b["date"] for b in bars] == ["2026-10-03", "2026-10-04", "2026-10-05"]


def test_fewer_bars_than_requested_are_returned_as_is(monkeypatch):
    new = [_bar(f"2026-10-{d:02d}", 200 + d) for d in range(1, 4)]
    _fake_alpaca(monkeypatch, {"sip": [(new, None)]})
    assert len(market_data._alpaca_bars("EFA", days=60)) == 3
