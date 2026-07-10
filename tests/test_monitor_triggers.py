"""Scheduler triggers: earnings proximity, DTE decay, invalidation,
cooldowns - and the digest."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.execution import executor
from midas.models import (
    ApprovalRequest, ApprovalStatus, ArtifactType, CheckpointTrigger,
    PipelineRun, Position, ResearchArtifact, RunKind, utcnow,
)
from midas.pipeline.committee import run_committee
from midas.scheduler.monitor import due_positions
from midas.outcomes.digest import build_digest


@pytest.fixture(autouse=True)
def demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)


def open_acme_position(factory, demo_router):
    run_committee(factory, demo_router, "ACME")
    with factory() as db:
        approval = db.scalar(select(ApprovalRequest).where(
            ApprovalRequest.status == ApprovalStatus.pending))
        executor.resolve_approval(db, approval, approve=True)
        db.commit()


def test_scheduled_trigger_when_review_due(factory, demo_router):
    open_acme_position(factory, demo_router)
    with factory() as db:
        p = db.scalar(select(Position))
        p.next_review_at = utcnow() - timedelta(hours=1)
        db.commit()
        due = due_positions(db)
        assert due and due[0][1] == CheckpointTrigger.scheduled


def test_earnings_trigger_beats_schedule(factory, demo_router):
    open_acme_position(factory, demo_router)
    with factory() as db:
        p = db.scalar(select(Position))
        p.next_review_at = utcnow() + timedelta(days=5)  # not due by calendar
        # plant a catalysts artifact with earnings inside the lookahead
        run = PipelineRun(kind=RunKind.committee, ticker="ACME")
        db.add(run)
        db.flush()
        db.add(ResearchArtifact(
            run_id=run.id, ticker="ACME", agent_slug="catalyst",
            artifact_type=ArtifactType.catalysts,
            content={"upcoming_earnings_date":
                     (date.today() + timedelta(days=3)).isoformat()},
            created_at=utcnow() + timedelta(minutes=1),  # strictly newest
        ))
        db.commit()
        due = due_positions(db)
        assert due and due[0][1] == CheckpointTrigger.earnings
        assert "earnings" in due[0][2]


def test_earnings_cooldown_after_review(factory, demo_router):
    open_acme_position(factory, demo_router)
    with factory() as db:
        p = db.scalar(select(Position))
        p.next_review_at = utcnow() + timedelta(days=5)
        p.last_reviewed_at = utcnow() - timedelta(hours=2)  # just reviewed
        run = PipelineRun(kind=RunKind.committee, ticker="ACME")
        db.add(run)
        db.flush()
        db.add(ResearchArtifact(
            run_id=run.id, ticker="ACME", agent_slug="catalyst",
            artifact_type=ArtifactType.catalysts,
            content={"upcoming_earnings_date":
                     (date.today() + timedelta(days=3)).isoformat()},
            created_at=utcnow() + timedelta(minutes=1),
        ))
        db.commit()
        assert due_positions(db) == []  # cooldown holds; invalidation would still fire


def test_dte_trigger_for_decaying_option(factory, demo_router):
    open_acme_position(factory, demo_router)
    with factory() as db:
        p = db.scalar(select(Position))
        p.next_review_at = utcnow() + timedelta(days=5)
        p.option_detail = {**p.option_detail,
                           "expiry": (date.today() + timedelta(days=6)).isoformat()}
        db.commit()
        due = due_positions(db)
        assert due and due[0][1] == CheckpointTrigger.dte


def test_digest_summarizes_the_week(factory, demo_router):
    open_acme_position(factory, demo_router)
    with factory() as db:
        digest = build_digest(db, days=7)
        assert digest["entries_filled"] and digest["entries_filled"][0]["approved_by"] == "operator"
        assert digest["open_positions"] and digest["open_positions"][0]["thesis"]
        assert digest["ai_spend_usd"] > 0
        assert digest["track_record"]["autonomy_level"] == 0
        assert digest["halts"] == []
