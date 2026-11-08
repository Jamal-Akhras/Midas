"""Discovery gating + structurer failure diagnosis."""

from datetime import datetime, timezone

from midas.config import settings
from midas.pipeline.screen import discover_tickers
from midas.risk import structurer_checks
from midas.scheduler import monitor


def test_discovery_disabled_paths(monkeypatch):
    monkeypatch.setattr(settings, "discovery_enabled", False)
    assert discover_tickers() == []
    monkeypatch.setattr(settings, "discovery_enabled", True)
    monkeypatch.setattr(settings, "alpaca_api_key", "")
    assert discover_tickers() == []


def test_failure_histogram_names_the_killer():
    chain = [
        # dies on DTE
        {
            "expiry": "2026-07-14",
            "dte": 4,
            "type": "call",
            "strike": 100,
            "bid": 1.0,
            "ask": 1.1,
            "mid": 1.05,
            "open_interest": 500,
            "volume": 10,
        },
        # dies on OI and one-sided market
        {
            "expiry": "2026-08-21",
            "dte": 42,
            "type": "call",
            "strike": 100,
            "bid": 0.0,
            "ask": 1.1,
            "mid": 0.55,
            "open_interest": 3,
            "volume": 0,
        },
        # dies on spread
        {
            "expiry": "2026-08-21",
            "dte": 42,
            "type": "call",
            "strike": 105,
            "bid": 1.0,
            "ask": 1.5,
            "mid": 1.25,
            "open_interest": 500,
            "volume": 5,
        },
    ]
    diag = structurer_checks.failure_histogram(chain)
    assert diag["contracts_seen"] == 3
    assert diag["failed_checks"]["min_dte"] == 1
    assert diag["failed_checks"]["open_interest"] == 1
    assert diag["failed_checks"]["has_market"] == 1
    assert diag["failed_checks"]["bid_ask_spread"] == 2  # no-bid contract fails spread too


def test_market_hours_gate(monkeypatch):
    monkeypatch.setattr(settings, "auto_cycle_market_hours_only", True)
    monkeypatch.setattr(settings, "auto_cycle_weekdays", "mon,tue,wed,thu,fri")
    monkeypatch.setattr(monitor, "utcnow", lambda: datetime(2026, 7, 8, 3, 0, tzinfo=timezone.utc))
    # off-hours: the gate must stop the cycle before it touches the DB
    monkeypatch.setattr(monitor, "_us_market_hours", lambda: False)
    assert monitor.maybe_auto_cycle(lambda: None, None) is None
    # the real hours function: Wednesday 03:00 UTC is overnight in New York
    real_hours = monitor._real_market_fns["hours"]
    assert real_hours() is False


def test_market_calendar_fallback_weekday(monkeypatch):
    from datetime import datetime, timezone

    real_day = monitor._real_market_fns["day"]
    monkeypatch.setattr(settings, "alpaca_api_key", "")
    monitor._calendar_cache.update(date="", open=True)
    monkeypatch.setattr(
        monitor, "utcnow", lambda: datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)
    )  # Saturday
    assert real_day() is False
    monitor._calendar_cache.update(date="", open=True)
    monkeypatch.setattr(
        monitor, "utcnow", lambda: datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)
    )  # Wednesday
    assert real_day() is True
    monitor._calendar_cache.update(date="", open=True)
