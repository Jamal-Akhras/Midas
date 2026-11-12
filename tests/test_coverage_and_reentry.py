"""2026-11-12: the event scan covers what the book holds, and a committee
re-entering a ticker sees its last exit.

The overnight scan covered the core ETFs and the watchlist but not NVDA,
the one open swing position. And a stopped-out name stays eligible for
the next day's screen with nothing in the screen context about the stop.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from midas.audit import record_event
from midas.config import settings
from midas.models import (
    CORE_BOOK,
    SWING_BOOK,
    ActorType,
    ArtifactType,
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
    ThesisStatus,
    TradeOutcome,
    TradeThesis,
    utcnow,
)
from midas.models.domain import Direction, PipelineRun, RunKind
from midas.pipeline import committee, events
from midas.tools import demo_data


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)


def _thesis(db, ticker, *, status=ThesisStatus.executed, book=SWING_BOOK):
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
        entry_zone_low=100.0,
        entry_zone_high=100.0,
        stop_price=95.0,
        targets=[110.0],
        status=status,
        book=book,
    )
    db.add(th)
    db.flush()
    return th


def _position(db, ticker, *, book=SWING_BOOK, status=PositionStatus.open):
    th = _thesis(db, ticker, book=book)
    p = Position(
        thesis_id=th.id,
        ticker=ticker,
        symbol=ticker,
        instrument=Instrument.shares,
        qty=1.0,
        avg_price=100.0,
        stop_price=95.0,
        last_mark_price=100.0,
        opened_at=utcnow() - timedelta(days=1),
        status=status,
        book=book,
    )
    db.add(p)
    db.flush()
    return p


# ---------- event scan coverage ----------


def test_event_scan_covers_held_and_approved_names(factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "event_scan_weekdays", utcnow().strftime("%a").lower()[:3])
    monkeypatch.setattr(settings, "core_universe", "SPY,QQQ")
    monkeypatch.setattr(settings, "watchlist", "RKLB,NVDA")
    with factory() as db:
        _position(db, "NVDA")  # held, also on the watchlist: once
        _position(db, "EFA", book=CORE_BOOK)  # the core book has its own universe
        _position(db, "PFE", status=PositionStatus.closed)  # closed: nothing to protect
        _thesis(db, "AXP", status=ThesisStatus.approved)  # approved, waiting for its zone
        _thesis(db, "TSLA", status=ThesisStatus.rejected)
        db.commit()
    scanned: list[list[str]] = []
    monkeypatch.setattr(
        events, "run_event_scan", lambda f, r, tickers: scanned.append(list(tickers)) or []
    )
    assert events.maybe_event_scan(factory, demo_router) == []
    assert scanned == [["SPY", "QQQ", "RKLB", "NVDA", "AXP"]]


# ---------- the recent exit in the screen context ----------


def _stopped_out(db, ticker="DUNE", *, days_ago=1):
    th = _thesis(db, ticker)
    th.stop_price = 159.43
    opened = utcnow() - timedelta(days=days_ago + 1)
    closed = utcnow() - timedelta(days=days_ago)
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
    db.add(
        Order(
            thesis_id=th.id,
            position_id=p.id,
            side=OrderSide.sell,
            instrument=Instrument.shares,
            symbol=ticker,
            qty=0.792,
            reason=OrderReason.exit,
            status=OrderStatus.filled,
            filled_price=155.61,
            filled_at=closed,
            created_at=closed,
        )
    )
    db.add(
        TradeOutcome(
            position_id=p.id,
            thesis_id=th.id,
            pnl_usd=-13.07,
            pnl_pct=-9.59,
            holding_days=1,
            exit_kind="stop",
            bull_case_right=False,
        )
    )
    record_event(
        db,
        event_type="stop.breach_exit",
        summary=f"{ticker}: underlying 155.6 breached stop 159.43 - deterministic exit",
        actor_type=ActorType.system,
        subject_type="position",
        subject_id=p.id,
    )
    db.flush()
    return p


def test_screen_context_shows_the_last_exit(db):
    _stopped_out(db)
    ctx = committee.screen_context_content(db, "DUNE", demo_data.daily_bars("DUNE", 60))
    exit_ = ctx["recent_exit"]
    assert exit_["exit_kind"] == "stop"
    assert exit_["entry"] == 172.116 and exit_["exit_price"] == 155.61
    assert exit_["pnl_pct"] == -9.59 and exit_["days_ago"] == 1
    assert "breached stop 159.43" in exit_["reason"]
    assert ctx["recent_exit_note"].startswith("RECENT EXIT: stopped out 1 day(s) ago at 155.61")
    assert "needs a reason the last exit did not have" in ctx["recent_exit_note"]


def test_no_recent_exit_is_said_plainly(db):
    ctx = committee.screen_context_content(db, "DUNE", demo_data.daily_bars("DUNE", 60))
    assert ctx["recent_exit"] is None
    assert ctx["recent_exit_note"] == "no swing exit in this ticker in the last 10 days"


def test_an_old_exit_is_not_recent(db):
    _stopped_out(db, days_ago=20)
    ctx = committee.screen_context_content(db, "DUNE", demo_data.daily_bars("DUNE", 60))
    assert ctx["recent_exit"] is None


def test_a_committee_reentering_the_ticker_reads_the_exit(factory, demo_router):
    with factory() as db:
        _stopped_out(db)
        db.commit()
    committee.run_committee(factory, demo_router, "DUNE")
    with factory() as db:
        prompts = db.execute(
            select(ModelCall.user_prompt)
            .join(CostEvent, ModelCall.cost_event_id == CostEvent.id)
            .where(CostEvent.agent_slug.in_(["triage", "investor"]))
        ).all()
        assert prompts and all("RECENT EXIT: stopped out" in p[0] for p in prompts)
