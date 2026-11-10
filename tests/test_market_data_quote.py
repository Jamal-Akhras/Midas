"""get_quote's price is the IEX latest trade: real-time, but IEX carries 3-10%
of the tape, so a name that has not printed there for a while reports an old
trade as 'latest' - RSG opened 2026-11-10 showing yesterday's close for 18+
minutes while the consolidated tape had already moved. When the IEX print is
older than the delayed consolidated feed's lag, that feed is asked too and the
newer trade wins. The result also says which feed and timestamp it came from."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from midas.config import settings
from midas.tools import market_data

NOW = datetime(2026, 11, 10, 14, 48, tzinfo=timezone.utc)


def _stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def _fake_quote_api(monkeypatch, trades: dict[str, dict | int]) -> list[str]:
    """`trades[feed]` is the trade body for that feed, or an HTTP status to
    refuse it with. Bars are served for any feed. Returns the feeds asked
    for on /trades/latest, in order."""
    asked: list[str] = []
    bars = [
        {"t": "2026-11-06T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": 246.85, "v": 1_810_581},
        {"t": "2026-11-09T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": 246.78, "v": 1_691_417},
    ]

    def fake_get(url, *, params=None, headers=None, timeout=None):
        req = httpx.Request("GET", url)
        if url.endswith("/trades/latest"):
            feed = str((params or {}).get("feed"))
            asked.append(feed)
            body = trades.get(feed, 403)
            if isinstance(body, int):
                return httpx.Response(body, json={"message": "subscription"}, request=req)
            return httpx.Response(200, json={"trade": body}, request=req)
        return httpx.Response(200, json={"bars": bars, "next_page_token": None}, request=req)

    monkeypatch.setattr(settings, "alpaca_api_key", "k")
    monkeypatch.setattr(settings, "alpaca_api_secret", "s")
    monkeypatch.setattr(settings, "finnhub_api_key", "")
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")  # live mode
    monkeypatch.setattr(market_data.httpx, "get", fake_get)
    monkeypatch.setattr(market_data, "_now", lambda: NOW)
    return asked


def test_fresh_iex_print_is_used_without_a_second_call(monkeypatch):
    asked = _fake_quote_api(monkeypatch, {"iex": {"p": 246.65, "t": _stamp(0.5)}})

    q = market_data.get_quote("rsg")

    assert q.source == "alpaca"
    assert q.data["price"] == 246.65
    assert q.data["feed"] == "iex"
    assert q.data["trade_at"] == _stamp(0.5)
    assert asked == ["iex"]
    # the rest of the shape is unchanged: callers read these by key
    assert q.data["ticker"] == "RSG"
    assert q.data["prev_close"] == 246.78
    assert q.data["avg_volume"] == pytest.approx((1_810_581 + 1_691_417) / 2)
    assert q.data["sector"] == ""


def test_stale_iex_print_is_replaced_by_a_newer_delayed_consolidated_trade(monkeypatch):
    asked = _fake_quote_api(
        monkeypatch,
        {
            "iex": {"p": 246.81, "t": _stamp(18 * 60)},  # yesterday's last IEX print
            "delayed_sip": {"p": 245.97, "t": _stamp(15)},
        },
    )

    q = market_data.get_quote("RSG")

    assert q.data["price"] == 245.97
    assert q.data["feed"] == "delayed_sip"
    assert q.data["trade_at"] == _stamp(15)
    assert asked == ["iex", "delayed_sip"]


def test_delayed_trade_that_is_older_than_the_iex_print_does_not_replace_it(monkeypatch):
    asked = _fake_quote_api(
        monkeypatch,
        {
            "iex": {"p": 100.0, "t": _stamp(20)},
            "delayed_sip": {"p": 99.0, "t": _stamp(40)},
        },
    )

    q = market_data.get_quote("XYZ")

    assert q.data["price"] == 100.0 and q.data["feed"] == "iex"
    assert asked == ["iex", "delayed_sip"]


def test_stale_iex_print_is_kept_when_the_delayed_feed_is_refused(monkeypatch):
    asked = _fake_quote_api(monkeypatch, {"iex": {"p": 246.81, "t": _stamp(60)}})

    q = market_data.get_quote("RSG")

    assert q.data["price"] == 246.81 and q.data["feed"] == "iex"
    assert asked == ["iex", "delayed_sip"]


def test_iex_print_without_a_timestamp_counts_as_stale(monkeypatch):
    asked = _fake_quote_api(
        monkeypatch,
        {"iex": {"p": 10.0}, "delayed_sip": {"p": 10.5, "t": _stamp(15)}},
    )

    q = market_data.get_quote("ABC")

    assert q.data["price"] == 10.5 and q.data["feed"] == "delayed_sip"
    assert asked == ["iex", "delayed_sip"]


def test_iex_refusal_still_falls_through_to_the_next_provider(monkeypatch):
    """An IEX 403 is a real failure (keys, plan), not a stale print: the
    existing fall-through to yfinance / the honest ToolError must stay."""
    _fake_quote_api(monkeypatch, {})
    monkeypatch.setattr(market_data, "_yf", lambda: None)
    with pytest.raises(market_data.ToolError):
        market_data.get_quote("RSG")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "2026-11-10T16:13:30.185396667Z",
            datetime(2026, 11, 10, 16, 13, 30, 185396, tzinfo=timezone.utc),
        ),
        ("2026-11-10T16:13:30Z", datetime(2026, 11, 10, 16, 13, 30, tzinfo=timezone.utc)),
        (
            "2026-11-10T16:13:30.5+00:00",
            datetime(2026, 11, 10, 16, 13, 30, 500000, tzinfo=timezone.utc),
        ),
        ("", None),
        (None, None),
        ("yesterday", None),
    ],
)
def test_alpaca_timestamps_parse_on_every_supported_python(raw, expected):
    """Alpaca stamps trades with nanoseconds and a Z suffix, which
    datetime.fromisoformat rejects before 3.11; the project supports 3.10."""
    assert market_data._parse_rfc3339(raw) == expected
