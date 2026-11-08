"""End-to-end: demo-mode committee run -> thesis -> approval -> paper fill
-> monitor re-evaluation -> exit -> outcome scoring. The demo provider runs
the SAME code path as production (tool loop, snapshots, schemas)."""
import pytest
from sqlalchemy import select

from midas.config import settings
from midas.execution import executor
from midas.models import (
    ApprovalRequest,
    ApprovalStatus,
    ArtifactType,
    DataSnapshot,
    Order,
    OrderStatus,
    Position,
    PositionStatus,
    ResearchArtifact,
    RunStatus,
    ThesisStatus,
    TradeOutcome,
    TradeThesis,
)
from midas.outcomes.scoring import score_closed_positions, stats
from midas.pipeline.committee import run_committee
from midas.pipeline.screen import run_screen
from midas.scheduler.monitor import run_monitor_once


@pytest.fixture(autouse=True)
def demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)


def test_screen_returns_floor_checked_candidates(factory, demo_router):
    content = run_screen(factory, demo_router)
    tickers = [c["ticker"] for c in content["candidates"]]
    assert "ACME" in tickers and "DUNE" in tickers
    assert all(c["price"] >= settings.min_price_usd for c in content["candidates"])


def test_full_committee_run_produces_audited_thesis(factory, demo_router):
    result = run_committee(factory, demo_router, "ACME")
    assert result.status == RunStatus.awaiting_approval
    assert result.thesis_id is not None

    with factory() as db:
        # every stage left an artifact
        types = {a.artifact_type for a in db.scalars(select(ResearchArtifact))}
        assert {ArtifactType.fundamentals, ArtifactType.dossier,
                ArtifactType.catalysts, ArtifactType.triage,
                ArtifactType.bull_case, ArtifactType.bear_case,
                ArtifactType.structure, ArtifactType.risk_check,
                ArtifactType.verdict} <= types
        # provenance: tool snapshots exist and research snapshots are
        # stamped with the artifact that consumed them
        snaps = db.scalars(select(DataSnapshot)).all()
        assert snaps
        research_snaps = [s for s in snaps if s.tool_name == "get_filings"]
        assert research_snaps and all(s.artifact_id for s in research_snaps)
        # the thesis is fully specified and awaiting the operator
        thesis = db.get(TradeThesis, result.thesis_id)
        assert thesis.status == ThesisStatus.proposed
        assert thesis.stop_price < thesis.entry_zone_low  # bullish sanity
        assert thesis.invalidators
        approval = db.scalar(select(ApprovalRequest).where(
            ApprovalRequest.status == ApprovalStatus.pending))
        assert approval is not None and approval.blocking


def test_approval_executes_on_paper_and_monitor_reviews(factory, demo_router):
    result = run_committee(factory, demo_router, "ACME")
    with factory() as db:
        approval = db.scalar(select(ApprovalRequest).where(
            ApprovalRequest.status == ApprovalStatus.pending))
        executor.resolve_approval(db, approval, approve=True)
        db.commit()

    with factory() as db:
        order = db.scalar(select(Order))
        assert order.status == OrderStatus.filled
        assert order.approved_by == "operator"
        assert order.risk_check["ok"] is True  # execution-time re-check recorded
        position = db.scalar(select(Position))
        assert position.status == PositionStatus.open
        thesis = db.get(TradeThesis, result.thesis_id)
        assert thesis.status == ThesisStatus.executed
        # force the scheduled review to be due now
        position.next_review_at = None
        db.commit()

    summary = run_monitor_once(factory, demo_router)
    assert summary["halted"] is None
    assert summary["reviewed"] and summary["reviewed"][0]["decision"] == "hold"

    with factory() as db:
        reeval = db.scalar(select(ResearchArtifact).where(
            ResearchArtifact.artifact_type == ArtifactType.reevaluation))
        assert reeval is not None
        position = db.scalar(select(Position))
        assert position.next_review_at is not None  # rescheduled


def test_exit_flow_scores_outcome(factory, demo_router):
    run_committee(factory, demo_router, "ACME")
    with factory() as db:
        approval = db.scalar(select(ApprovalRequest).where(
            ApprovalRequest.status == ApprovalStatus.pending))
        executor.resolve_approval(db, approval, approve=True)
        db.commit()

    with factory() as db:
        position = db.scalar(select(Position))
        # L0: an exit decision becomes a blocking approval, not an order
        req = executor.propose_or_execute_exit(
            db, position, decision="exit", reasons=["thesis invalidated"])
        assert isinstance(req, ApprovalRequest)
        executor.resolve_approval(db, req, approve=True)
        db.commit()

    with factory() as db:
        position = db.scalar(select(Position))
        assert position.status == PositionStatus.closed
        scored = score_closed_positions(db)
        assert len(scored) == 1
        outcome = db.scalar(select(TradeOutcome))
        assert outcome.holding_days >= 0
        s = stats(db)
        assert s["closed_positions"] == 1
        assert s["l3_bar_met"]["closed_positions"] is False  # 1 < 20


def test_resume_skips_completed_stages(factory, demo_router):
    result = run_committee(factory, demo_router, "ACME")
    with factory() as db:
        artifact_count = len(db.scalars(select(ResearchArtifact)).all())
    # resuming the same run must not duplicate artifacts
    result2 = run_committee(factory, demo_router, "ACME",
                            resume_run_id=result.run_id)
    with factory() as db:
        assert len(db.scalars(select(ResearchArtifact)).all()) == artifact_count
    assert result2.run_id == result.run_id


def test_floor_gate_rejects_penny_ticker(factory, demo_router):
    result = run_committee(factory, demo_router, "EMBR")  # $3.15 < $5 floor
    assert result.status == RunStatus.done
    assert "floors" in result.note
    with factory() as db:
        triage = db.scalar(select(ResearchArtifact).where(
            ResearchArtifact.artifact_type == ArtifactType.triage))
        assert triage.content["passable"] is False
        # no expensive research ran
        assert db.scalar(select(ResearchArtifact).where(
            ResearchArtifact.artifact_type == ArtifactType.dossier)) is None
