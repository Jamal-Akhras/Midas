"""Runtime integrity gates: the two cases must come from different providers,
and a trade verdict's levels must make sense against the live quote."""

import pytest
from sqlalchemy import select

from midas.agents.schemas import VerdictOutput
from midas.config import settings
from midas.models import (
    ArtifactType,
    AuditEvent,
    CostEvent,
    ModelCall,
    PipelineRun,
    ResearchArtifact,
    RunKind,
    RunStatus,
    TradeThesis,
)
from midas.pipeline.committee import run_committee
from midas.pipeline.runner import stage_provider
from midas.providers import demo_provider
from midas.risk import verdict_checks


def _verdict(**kw) -> VerdictOutput:
    d = dict(
        ticker="ACME",
        verdict="trade",
        direction="bullish",
        instrument="shares",
        entry_zone_low=100.0,
        entry_zone_high=105.0,
        stop_price=92.0,
        targets=[120.0],
        thesis_summary="x",
        what_invalidates_this=["stop"],
    )
    d.update(kw)
    return VerdictOutput(**d)


# ---------- sanity checks ----------


def test_sane_verdicts_pass():
    assert verdict_checks.problems(_verdict(), spot=102.0) == []
    bear = _verdict(direction="bearish", stop_price=112.0, targets=[85.0])
    assert verdict_checks.problems(bear, spot=101.0) == []


def test_the_googl_hallucination_is_caught():
    bad = _verdict(entry_zone_low=135.0, entry_zone_high=145.0, stop_price=20.85, targets=[12.0])
    issues = verdict_checks.problems(bad, spot=346.87)
    assert len(issues) == 3
    assert any("live quote" in i for i in issues)
    assert any("maximum" in i for i in issues)
    assert any("targets" in i for i in issues)


def test_stop_distance_and_zone_shape():
    tight = _verdict(entry_zone_low=104.0, entry_zone_high=105.0, stop_price=103.5)  # 1.4% below
    assert any("minimum" in i for i in verdict_checks.problems(tight, 104.5))
    assert any("not below" in i for i in verdict_checks.problems(_verdict(stop_price=101.0), 102.0))
    inverted = _verdict(entry_zone_low=105.0, entry_zone_high=100.0)
    assert any("inverted" in i for i in verdict_checks.problems(inverted, 102.0))


def test_bearish_mirror_rules():
    bear = _verdict(direction="bearish", stop_price=103.0, targets=[120.0])
    issues = verdict_checks.problems(bear, spot=101.0)
    assert any("not above" in i for i in issues) and any("not below" in i for i in issues)


def test_no_trade_is_never_checked():
    v = VerdictOutput(ticker="ACME", verdict="no_trade", thesis_summary="x", no_trade_reason="no")
    assert verdict_checks.problems(v, spot=1.0) == []


# ---------- provider diversity ----------


def test_stage_provider_reads_the_routers_decision(db, demo_router):
    run = PipelineRun(kind=RunKind.committee, ticker="ACME")
    db.add(run)
    db.flush()
    for slug, model in (("bull", "claude-sonnet-5-5"), ("bear", "gpt-4o")):
        ev = CostEvent(run_id=run.id, agent_slug=slug, provider="demo", model=model)
        db.add(ev)
        db.flush()
        db.add(ModelCall(cost_event_id=ev.id, parse_ok=True, router_decision={"chosen": model}))
    db.flush()
    assert stage_provider(db, demo_router, run.id, "bull") == "anthropic"
    assert stage_provider(db, demo_router, run.id, "bear") == "openai"
    assert stage_provider(db, demo_router, run.id, "investor") is None


@pytest.fixture()
def demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)
    monkeypatch.setattr(settings, "require_case_provider_diversity", True)


def _audit(db, event_type: str) -> list[AuditEvent]:
    return db.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type)).all()


def test_demo_committee_passes_the_diversity_gate(demo_mode, factory, demo_router):
    result = run_committee(factory, demo_router, "ACME")
    assert result.status == RunStatus.awaiting_approval
    with factory() as db:
        assert _audit(db, "committee.provider_collision") == []
        assert _audit(db, "verdict.rejected_sanity") == []


def test_same_provider_cases_refuse_the_run(demo_mode, factory, demo_router):
    demo_router.task_types["bear_case"] = list(demo_router.task_types["bull_case"])
    result = run_committee(factory, demo_router, "ACME")
    assert result.status == RunStatus.failed
    assert "REQUIRE_CASE_PROVIDER_DIVERSITY" in result.note
    with factory() as db:
        events = _audit(db, "committee.provider_collision")
        assert len(events) == 1 and events[0].payload == {"bull": "anthropic", "bear": "anthropic"}
        # the expensive stages after the cases never ran, and no thesis exists
        types = {a.artifact_type for a in db.scalars(select(ResearchArtifact))}
        assert ArtifactType.verdict not in types and ArtifactType.structure not in types
        assert db.scalar(select(TradeThesis)) is None


def test_diversity_gate_can_be_switched_off(demo_mode, factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "require_case_provider_diversity", False)
    demo_router.task_types["bear_case"] = list(demo_router.task_types["bull_case"])
    result = run_committee(factory, demo_router, "ACME")
    assert result.status == RunStatus.awaiting_approval


def test_hallucinated_verdict_is_reprompted_then_refused(
    demo_mode, factory, demo_router, monkeypatch
):
    calls, _ = demo_provider.SCRIPTS["investor"]

    def hallucinated(ticker: str) -> dict:
        return {
            **demo_provider._verdict(ticker),
            "entry_zone_low": 135.0,
            "entry_zone_high": 145.0,
            "stop_price": 20.85,
            "targets": [12.0],
        }

    monkeypatch.setitem(demo_provider.SCRIPTS, "investor", (calls, hallucinated))
    result = run_committee(factory, demo_router, "ACME")
    assert result.status == RunStatus.done
    assert result.note.startswith("verdict failed the sanity gate twice")
    with factory() as db:
        assert db.scalar(select(TradeThesis)) is None
        verdicts = db.scalars(
            select(ResearchArtifact).where(ResearchArtifact.artifact_type == ArtifactType.verdict)
        ).all()
        assert sorted(v.version for v in verdicts) == [1, 2]  # re-prompted once, both stored
        rejections = _audit(db, "verdict.rejected_sanity")
        assert len(rejections) == 2
        assert rejections[0].payload["live_quote"] == pytest.approx(184.20)
