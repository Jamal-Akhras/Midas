"""2026-11-12: the portfolio manager cannot re-litigate a verdict.

AXP filled at 18:57:57 local; the daily portfolio review started four
seconds later, read the bull and bear cases written a minute before, and
exited on "insider selling and a congressional report" - facts the
committee had weighed when it said trade. Now a position opened inside
PM_EXIT_MIN_AGE_HOURS is exempt from PM exits until a research artifact or
event flag newer than its entry exists, and the review runs at a fixed
time of day rather than on whichever pass first finds a position."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.execution import paper_broker
from midas.models import (
    SWING_BOOK,
    ArtifactType,
    AuditEvent,
    EventFlag,
    Instrument,
    Order,
    Position,
    PositionStatus,
    ResearchArtifact,
    ThesisStatus,
    TradeThesis,
    utcnow,
)
from midas.models.domain import Direction, PipelineRun, RunKind
from midas.providers.router import ModelRouter
from midas.scheduler import monitor
from tests.conftest import FakeProvider


@pytest.fixture(autouse=True)
def _l2(monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 2)
    monkeypatch.setattr(settings, "fractional_shares", True)
    monkeypatch.setattr(settings, "regime_filter_enabled", False)
    monkeypatch.setattr(settings, "core_enabled", False)
    monkeypatch.setattr(settings, "pm_exit_min_age_hours", 24)
    monkeypatch.setattr(monitor, "_pm_review_time_reached", lambda: True)
    import midas.tools.market_data as md

    class _Q:
        data = {"price": 322.5, "sector": "Financial Services"}

    monkeypatch.setattr(md, "get_quote", lambda t: _Q())
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 322.5)


def _committee_artifacts(db, ticker, *, at):
    """The committee's own research, dated BEFORE the fill like the real thing."""
    run = PipelineRun(kind=RunKind.committee, ticker=ticker)
    db.add(run)
    db.flush()
    verdict = ResearchArtifact(
        run_id=run.id,
        ticker=ticker,
        agent_slug="investor",
        artifact_type=ArtifactType.verdict,
        content={"verdict": "trade", "thesis_summary": "post-earnings drift"},
        created_at=at,
    )
    db.add(verdict)
    db.add(
        ResearchArtifact(
            run_id=run.id,
            ticker=ticker,
            agent_slug="bear",
            artifact_type=ArtifactType.bear_case,
            content={
                "conviction": 6.0,
                "key_evidence": ["CEO sold $99M of stock (dossier)", "congressional report"],
            },
            created_at=at,
        )
    )
    db.flush()
    return run, verdict


def _open_position(db, ticker="AXP", *, age_minutes=5, price=322.99):
    opened_at = utcnow() - timedelta(minutes=age_minutes)
    run, verdict = _committee_artifacts(db, ticker, at=opened_at - timedelta(minutes=1))
    th = TradeThesis(
        run_id=run.id,
        verdict_artifact_id=verdict.id,
        ticker=ticker,
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=price,
        entry_zone_high=price,
        stop_price=303.53,
        targets=[335.1],
        status=ThesisStatus.executed,
        book=SWING_BOOK,
        thesis_summary="post-earnings drift",
        invalidators=["significant NEW insider selling"],
    )
    db.add(th)
    db.flush()
    p = Position(
        thesis_id=th.id,
        ticker=ticker,
        symbol=ticker,
        instrument=Instrument.shares,
        qty=0.46,
        avg_price=price,
        stop_price=303.53,
        last_mark_price=price,
        opened_at=opened_at,
        sector="Financial Services",
    )
    db.add(p)
    db.flush()
    return p


def _pm_says_exit(ticker="AXP"):
    answer = json.dumps(
        {
            "assessment": "one position, facing insider selling and a congressional report",
            "actions": [
                {
                    "ticker": ticker,
                    "decision": "exit",
                    "reasons": ["CEO sold $99M of stock", "congressional report"],
                }
            ],
            "rebalance_note": "",
        }
    )
    # the PM has tools: one reply ends the tool loop, the next is the answer
    provider = FakeProvider("openai", ["no further research needed", answer])
    return provider, ModelRouter(providers={"openai": provider})


def _book(factory, **kw):
    with factory() as db:
        paper_broker.set_account(db, cash=800.0, equity=989.0)
        p = _open_position(db, **kw)
        db.commit()
        return p.id


def _sell_orders(db):
    return db.scalars(select(Order).where(Order.symbol == "AXP")).all()


# ---------- the guard ----------


def test_pm_cannot_exit_a_fresh_position_on_the_facts_it_was_opened_on(factory):
    pid = _book(factory, age_minutes=5)
    provider, router = _pm_says_exit()
    results = monitor.maybe_portfolio_review(factory, router)
    assert results["AXP"].startswith("exit refused")
    with factory() as db:
        p = db.get(Position, pid)
        assert p.status == PositionStatus.open and p.qty == 0.46
        assert _sell_orders(db) == []
        refused = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "portfolio.action_refused")
        )
        assert refused is not None
        assert "AXP" in refused.summary and "newer than" in refused.summary
        assert refused.payload["reasons"] == ["CEO sold $99M of stock", "congressional report"]
    # the PM was told the position was off-limits for an exit
    prompt = str(provider.calls[0]["messages"][-1]["content"])
    assert '"exit_allowed": false' in prompt
    assert "newer than its entry" in prompt


def test_a_newer_event_flag_unlocks_the_exit(factory):
    pid = _book(factory, age_minutes=5)
    with factory() as db:
        db.add(
            EventFlag(
                ticker="AXP",
                kind="regulatory",
                severity="high",
                direction="negative",
                summary="CFPB opens an enforcement action",
                source_url="https://example.test/cfpb",
                created_at=utcnow(),
            )
        )
        db.commit()
    _, router = _pm_says_exit()
    results = monitor.maybe_portfolio_review(factory, router)
    assert results["AXP"].startswith("exit routed")
    with factory() as db:
        assert db.get(Position, pid).status == PositionStatus.closed
        assert len(_sell_orders(db)) == 1


def test_newer_research_on_the_ticker_unlocks_the_exit(factory):
    pid = _book(factory, age_minutes=5)
    with factory() as db:
        run = PipelineRun(kind=RunKind.event_scan, ticker="AXP")
        db.add(run)
        db.flush()
        db.add(
            ResearchArtifact(
                run_id=run.id,
                ticker="AXP",
                agent_slug="catalyst",
                artifact_type=ArtifactType.catalysts,
                content={"catalysts": []},
                created_at=utcnow(),
            )
        )
        db.commit()
    _, router = _pm_says_exit()
    results = monitor.maybe_portfolio_review(factory, router)
    assert results["AXP"].startswith("exit routed")
    with factory() as db:
        assert db.get(Position, pid).status == PositionStatus.closed


def test_the_systems_own_reviews_do_not_count_as_new_facts(factory):
    """A reevaluation or post-mortem is the system's opinion, not a fact
    newer than the entry; only research and event flags unlock the PM."""
    pid = _book(factory, age_minutes=5)
    with factory() as db:
        run = PipelineRun(kind=RunKind.reevaluation, ticker="AXP")
        db.add(run)
        db.flush()
        db.add(
            ResearchArtifact(
                run_id=run.id,
                ticker="AXP",
                agent_slug="reevaluator",
                artifact_type=ArtifactType.reevaluation,
                content={"decision": "hold"},
                created_at=utcnow(),
            )
        )
        db.commit()
    _, router = _pm_says_exit()
    results = monitor.maybe_portfolio_review(factory, router)
    assert results["AXP"].startswith("exit refused")
    with factory() as db:
        assert db.get(Position, pid).status == PositionStatus.open


def test_an_old_position_can_be_exited_on_judgment(factory):
    pid = _book(factory, age_minutes=26 * 60)
    _, router = _pm_says_exit()
    results = monitor.maybe_portfolio_review(factory, router)
    assert results["AXP"].startswith("exit routed")
    with factory() as db:
        assert db.get(Position, pid).status == PositionStatus.closed


def test_pm_review_waits_for_its_time_of_day(factory, monkeypatch):
    _book(factory, age_minutes=26 * 60)
    monkeypatch.setattr(monitor, "_pm_review_time_reached", lambda: False)
    provider, router = _pm_says_exit()
    assert monitor.maybe_portfolio_review(factory, router) is None
    assert provider.calls == []
    with factory() as db:  # the time gate is checked before a run row exists
        assert (
            db.scalar(select(PipelineRun).where(PipelineRun.kind == RunKind.portfolio_review))
            is None
        )


def test_review_time_is_read_as_new_york_clock(monkeypatch):
    from datetime import datetime, timezone

    real = monitor._real_market_fns["pm_time"]  # conftest pins the module attribute
    monkeypatch.setattr(settings, "portfolio_review_after_et", "11:30")
    # 16:29 UTC on a November day = 11:29 New York (EST)
    monkeypatch.setattr(
        monitor, "utcnow", lambda: datetime(2026, 11, 12, 16, 29, tzinfo=timezone.utc)
    )
    assert real() is False
    monkeypatch.setattr(
        monitor, "utcnow", lambda: datetime(2026, 11, 12, 16, 31, tzinfo=timezone.utc)
    )
    assert real() is True
    monkeypatch.setattr(settings, "portfolio_review_after_et", "")
    assert real() is True  # no time = no gate


def test_guard_age_is_part_of_the_strategy_fingerprint(monkeypatch):
    before = monitor._strategy_fingerprint()
    monkeypatch.setattr(settings, "pm_exit_min_age_hours", 0)
    assert monitor._strategy_fingerprint() != before


def test_guard_off_restores_the_old_behaviour(factory, monkeypatch):
    monkeypatch.setattr(settings, "pm_exit_min_age_hours", 0)
    pid = _book(factory, age_minutes=5)
    _, router = _pm_says_exit()
    results = monitor.maybe_portfolio_review(factory, router)
    assert results["AXP"].startswith("exit routed")
    with factory() as db:
        assert db.get(Position, pid).status == PositionStatus.closed
