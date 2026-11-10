"""The core book reads its trend signal from COMPLETED daily bars. The bar
for the session in progress (dated today in New York) is dropped before the
200-day comparison, so the live rule reads the prior close the way the
backtest does; the live quote is used for sizing only."""

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.models import AuditEvent, ResearchArtifact
from midas.strategy import core
from midas.tools.base import ToolResult
from midas.tools.registry import TOOLS


@pytest.fixture(autouse=True)
def core_settings(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 1)
    monkeypatch.setattr(settings, "core_enabled", True)
    monkeypatch.setattr(settings, "core_universe", "ACME,BOLT,DUNE")
    monkeypatch.setattr(settings, "core_allocation_pct", 60.0)
    monkeypatch.setattr(settings, "core_trend_window", 20)
    monkeypatch.setattr(settings, "fractional_shares", True)


def _bars(closes: list[float], last_date: date) -> list[dict]:
    n = len(closes)
    return [
        {
            "date": (last_date - timedelta(days=n - 1 - i)).isoformat(),
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": 1_000_000,
        }
        for i, c in enumerate(closes)
    ]


def test_completed_bars_drops_only_a_trailing_same_day_bar():
    bars = _bars([1.0, 2.0, 3.0], date(2026, 12, 1))
    assert core.completed_bars(bars, today="2026-12-01") == bars[:2]
    assert core.completed_bars(bars, today="2026-12-02") == bars
    assert core.completed_bars([], today="2026-12-01") == []
    # only a TRAILING bar is dropped: an earlier bar with that date is left alone
    assert core.completed_bars(bars, today="2026-11-30") == bars


def test_today_et_is_an_iso_date():
    assert date.fromisoformat(core.today_et()).isoformat() == core.today_et()


def test_session_in_progress_bar_does_not_decide_the_signal(db, monkeypatch):
    """The EFA case of 2026-11-09: the prior close sat below the average and
    the slice was bought only because the opening print gapped above it."""
    today = date.fromisoformat(core.today_et())
    completed = [100.0] * 19 + [95.0]  # 20-day average 99.75; last completed close 95

    def gapped_open(ticker: str, days: int = 60) -> ToolResult:
        return ToolResult(data=_bars([*completed, 110.0], today), source="test")

    monkeypatch.setattr(TOOLS["get_daily_bars"], "fn", gapped_open)
    plan = core.build_plan(db)

    assert plan.signal_date == (today - timedelta(days=1)).isoformat()
    for ticker in ("ACME", "BOLT", "DUNE"):
        assert plan.targets[ticker] == 0.0, ticker
        assert plan.trend[ticker]["close"] == 95.0
        assert plan.trend[ticker]["above"] is False
        assert plan.trend[ticker]["signal_date"] == plan.signal_date
    assert plan.orders == []


def test_bars_that_end_before_today_are_used_as_is(db, monkeypatch):
    today = date.fromisoformat(core.today_et())
    yesterday = today - timedelta(days=1)

    def rising(ticker: str, days: int = 60) -> ToolResult:
        return ToolResult(data=_bars([100.0 + i for i in range(25)], yesterday), source="test")

    monkeypatch.setattr(TOOLS["get_daily_bars"], "fn", rising)
    plan = core.build_plan(db)
    assert plan.signal_date == yesterday.isoformat()
    assert all(w == pytest.approx(1 / 3) for w in plan.targets.values())
    assert plan.trend["ACME"]["close"] == 124.0


def test_signal_date_is_recorded_in_the_artifact_and_the_planned_event(db):
    result = core.run_rebalance(db, force=True)
    db.commit()
    assert result["status"] == "executed"
    artifact = db.get(ResearchArtifact, uuid.UUID(result["artifact_id"]))
    assert artifact is not None
    signal_date = artifact.content["signal_date"]
    assert date.fromisoformat(signal_date)  # a real date, not ""
    assert result["signal_date"] == signal_date
    planned = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "core.planned"))
    assert planned is not None and planned.payload["signal_date"] == signal_date
    restored = core.CorePlan.from_content(artifact.content)
    assert restored.signal_date == signal_date
    # plans stored before the field existed still load
    assert core.CorePlan.from_content({**artifact.content, "signal_date": None}).signal_date == ""
    legacy = {k: v for k, v in artifact.content.items() if k != "signal_date"}
    assert core.CorePlan.from_content(legacy).signal_date == ""
