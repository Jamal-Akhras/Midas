"""2026-11-12: the post-mortem reads the whole record.

It was handed the thesis, the position and the P&L - no price path, no
gap flag, no review decisions - and wrote "negative earnings surprise" for
a stock whose earnings were five days away. Now it gets the daily bars
from entry to exit, every audit event and review on the position, and the
orders, plus a rule: a cause the record does not state is not a cause."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from midas.agents.defs import PROMPTS_DIR
from midas.audit import record_event
from midas.config import settings
from midas.models import (
    SWING_BOOK,
    ActorType,
    ArtifactType,
    CheckpointStatus,
    CheckpointTrigger,
    CostEvent,
    Instrument,
    ModelCall,
    Order,
    OrderReason,
    OrderSide,
    OrderStatus,
    Position,
    PositionStatus,
    ResearchArtifact,
    ReviewCheckpoint,
    ThesisStatus,
    TradeOutcome,
    TradeThesis,
    utcnow,
)
from midas.models.domain import Direction, PipelineRun, RunKind
from midas.scheduler import monitor


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)


def _closed_trade(db, ticker="DUNE"):
    run = PipelineRun(kind=RunKind.committee, ticker=ticker)
    db.add(run)
    db.flush()
    art = ResearchArtifact(
        run_id=run.id,
        ticker=ticker,
        agent_slug="investor",
        artifact_type=ArtifactType.verdict,
        content={},
    )
    db.add(art)
    db.flush()
    th = TradeThesis(
        run_id=run.id,
        verdict_artifact_id=art.id,
        ticker=ticker,
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=168.89,
        entry_zone_high=172.09,
        stop_price=159.43,
        targets=[190.0],
        status=ThesisStatus.executed,
        book=SWING_BOOK,
        thesis_summary="rebound into earnings",
        invalidators=["Negative earnings surprise on November 17th", "price below 159.43"],
    )
    db.add(th)
    db.flush()
    opened = utcnow() - timedelta(days=2)
    closed = utcnow() - timedelta(days=1)
    p = Position(
        thesis_id=th.id,
        ticker=ticker,
        symbol=ticker,
        instrument=Instrument.shares,
        qty=0.0,
        avg_price=172.116,
        stop_price=159.43,
        last_mark_price=156.0,
        opened_at=opened,
        closed_at=closed,
        status=PositionStatus.closed,
        realized_pnl_usd=-13.07,
    )
    db.add(p)
    db.flush()
    for side, price, reason, at in (
        (OrderSide.buy, 172.116, OrderReason.entry, opened),
        (OrderSide.sell, 155.61, OrderReason.exit, closed),
    ):
        db.add(
            Order(
                thesis_id=th.id,
                position_id=p.id,
                side=side,
                instrument=Instrument.shares,
                symbol=ticker,
                qty=0.792,
                reason=reason,
                status=OrderStatus.filled,
                filled_price=price,
                filled_at=at,
                created_at=at,
            )
        )
    db.add(
        ReviewCheckpoint(
            position_id=p.id,
            due_at=opened,
            trigger=CheckpointTrigger.scheduled,
            status=CheckpointStatus.done,
            decision="hold",
            notes="earnings on 2026-11-17 (6 day(s) away)",
        )
    )
    record_event(
        db,
        event_type="premarket.gap_flagged",
        summary=f"{ticker}: premarket -5.8% vs prev close - review scheduled for the open",
        actor_type=ActorType.system,
        subject_type="position",
        subject_id=p.id,
    )
    record_event(
        db,
        event_type="stop.breach_exit",
        summary=f"{ticker}: underlying 155.6 breached stop 159.43 - deterministic exit",
        actor_type=ActorType.system,
        subject_type="position",
        subject_id=p.id,
    )
    outcome = TradeOutcome(
        position_id=p.id,
        thesis_id=th.id,
        pnl_usd=-13.07,
        pnl_pct=-9.59,
        holding_days=1,
        exit_kind="stop",
        bull_case_right=False,
    )
    db.add(outcome)
    db.flush()
    return outcome


def _postmortem_prompt(db) -> str:
    row = db.execute(
        select(ModelCall.user_prompt)
        .join(CostEvent, ModelCall.cost_event_id == CostEvent.id)
        .where(CostEvent.agent_slug == "postmortem")
        .order_by(ModelCall.created_at.desc())
    ).first()
    assert row is not None
    return row[0]


def test_postmortem_is_handed_the_whole_record(factory, demo_router):
    with factory() as db:
        oid = _closed_trade(db).id
        db.commit()
    lessons = monitor.run_post_mortems(factory, demo_router, [oid])
    assert lessons
    with factory() as db:
        prompt = _postmortem_prompt(db)
    assert '"price_path"' in prompt and '"close"' in prompt
    opened_day = (utcnow() - timedelta(days=2)).date().isoformat()
    assert opened_day in prompt  # the bar for the entry day is there
    assert "premarket -5.8%" in prompt
    assert "breached stop 159.43" in prompt
    assert '"decision": "hold"' in prompt and "6 day(s) away" in prompt
    assert '"side": "buy"' in prompt and "155.61" in prompt
    assert "the cause is not in the record" in prompt


def test_price_path_is_bounded_to_the_holding_window(factory, demo_router):
    with factory() as db:
        oid = _closed_trade(db).id
        db.commit()
    monitor.run_post_mortems(factory, demo_router, [oid])
    with factory() as db:
        prompt = _postmortem_prompt(db)
    long_ago = (utcnow() - timedelta(days=30)).date().isoformat()
    assert long_ago not in prompt


def test_record_survives_a_bars_outage(factory, demo_router, monkeypatch):
    import midas.tools.market_data as md

    def _down(ticker, days=60):
        raise RuntimeError("feed down")

    monkeypatch.setattr(md, "get_daily_bars", _down)
    with factory() as db:
        oid = _closed_trade(db).id
        db.commit()
    assert monitor.run_post_mortems(factory, demo_router, [oid])
    with factory() as db:
        prompt = _postmortem_prompt(db)
    assert "price path unavailable" in prompt


def test_prompt_forbids_inventing_a_cause():
    text = (PROMPTS_DIR / "postmortem.md").read_text(encoding="utf-8")
    assert "not in the record" in text
