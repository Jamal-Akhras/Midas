"""Self-funding metric, gate-before-deep-research ordering, and the
auto-cycle cadence."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.models import (
    ArtifactType,
    CostEvent,
    Direction,
    Instrument,
    PipelineRun,
    Position,
    PositionStatus,
    ResearchArtifact,
    RunKind,
    ThesisStatus,
    TradeOutcome,
    TradeThesis,
    aware,
    utcnow,
)
from midas.outcomes.scoring import stats
from midas.pipeline.committee import run_committee
from midas.scheduler.monitor import maybe_auto_cycle, run_monitor_once


@pytest.fixture(autouse=True)
def demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)


def _outcome(db, pnl, days_ago=0):
    run = PipelineRun(kind=RunKind.committee, ticker="ACME")
    db.add(run)
    db.flush()
    art = ResearchArtifact(run_id=run.id, ticker="ACME", agent_slug="investor",
                           artifact_type=ArtifactType.verdict, content={})
    db.add(art)
    db.flush()
    thesis = TradeThesis(run_id=run.id, verdict_artifact_id=art.id, ticker="ACME",
                         direction=Direction.bullish, instrument=Instrument.shares,
                         entry_zone_low=99, entry_zone_high=101, stop_price=95,
                         status=ThesisStatus.closed)
    db.add(thesis)
    db.flush()
    p = Position(thesis_id=thesis.id, ticker="ACME", symbol="ACME",
                 instrument=Instrument.shares, qty=0, avg_price=100,
                 status=PositionStatus.closed, opened_at=utcnow(),
                 closed_at=utcnow())
    db.add(p)
    db.flush()
    o = TradeOutcome(position_id=p.id, thesis_id=thesis.id, pnl_usd=pnl,
                     pnl_pct=pnl, holding_days=1, exit_kind="manual")
    db.add(o)
    db.flush()
    o.created_at = utcnow() - timedelta(days=days_ago)
    db.flush()
    return o


def test_self_funding_ratio_and_window(db):
    db.add(CostEvent(provider="demo", model="m", cost_usd=2.0))
    db.flush()
    _outcome(db, pnl=10.0)              # inside the 30d window
    _outcome(db, pnl=999.0, days_ago=45)  # outside - must not count
    s = stats(db)["self_funding"]
    assert s["realized_pnl_30d"] == 10.0
    assert s["ai_spend_30d"] == 2.0
    assert s["ratio"] == 5.0
    assert s["covers_costs"] is True


def test_self_funding_negative_pnl_does_not_cover(db):
    db.add(CostEvent(provider="demo", model="m", cost_usd=1.0))
    db.flush()
    _outcome(db, pnl=-50.0)
    s = stats(db)["self_funding"]
    assert s["covers_costs"] is False
    assert s["ratio"] == -50.0


def test_no_spend_means_no_ratio(db):
    s = stats(db)["self_funding"]
    assert s["ratio"] is None and s["covers_costs"] is False


def test_gate_runs_before_deep_research(factory, demo_router):
    run_committee(factory, demo_router, "ACME")
    with factory() as db:
        def created(at):
            art = db.scalar(select(ResearchArtifact).where(
                ResearchArtifact.artifact_type == at))
            return aware(art.created_at)
        assert created(ArtifactType.triage) < created(ArtifactType.dossier), \
            "the cheap gate must run before the expensive dossier"


def test_auto_cycle_runs_once_per_configured_day(factory, demo_router, monkeypatch):
    today = utcnow().strftime("%a").lower()[:3]
    monkeypatch.setattr(settings, "auto_cycle_weekdays", today)
    monkeypatch.setattr(settings, "auto_cycle_market_hours_only", False)
    result = maybe_auto_cycle(factory, demo_router)
    assert result is not None
    assert result["screened"] == 2  # demo screen keeps ACME + DUNE
    assert set(result["committees"]) == {"ACME", "DUNE"}
    # second call the same day: skipped
    assert maybe_auto_cycle(factory, demo_router) is None


def test_auto_cycle_off_by_default_and_off_days(factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "auto_cycle_weekdays", "")
    monkeypatch.setattr(settings, "auto_cycle_market_hours_only", False)
    assert maybe_auto_cycle(factory, demo_router) is None
    not_today = "mon" if utcnow().strftime("%a").lower()[:3] != "mon" else "tue"
    monkeypatch.setattr(settings, "auto_cycle_weekdays", not_today)
    monkeypatch.setattr(settings, "auto_cycle_market_hours_only", False)
    assert maybe_auto_cycle(factory, demo_router) is None


def test_monitor_reports_auto_cycle(factory, demo_router, monkeypatch):
    today = utcnow().strftime("%a").lower()[:3]
    monkeypatch.setattr(settings, "auto_cycle_weekdays", today)
    monkeypatch.setattr(settings, "auto_cycle_market_hours_only", False)
    summary = run_monitor_once(factory, demo_router)
    assert summary["auto_cycle"] is not None
    assert summary["auto_cycle"]["screened"] == 2
