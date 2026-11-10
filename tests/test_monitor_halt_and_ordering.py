"""2026-11-10 review, step 1: the monitor pass.

R4: one failing entry cannot roll back another position's fill. R5: a loss
halt stops new risk, not risk management. R6: stop-breach exits run before
any LLM work. R7: the entry queue serves only the swing book. R9: a ticker
the committee just ruled no_trade is not re-researched during a cooldown."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from midas.config import settings
from midas.execution import paper_broker
from midas.models import (
    CORE_BOOK,
    SWING_BOOK,
    ArtifactType,
    EventFlag,
    Instrument,
    Order,
    OrderStatus,
    Position,
    PositionStatus,
    RunStatus,
    SystemState,
    TradeThesis,
    utcnow,
)
from midas.models.domain import Direction, PipelineRun, ResearchArtifact, RunKind, ThesisStatus
from midas.risk import engine as risk_engine
from midas.scheduler import monitor
from midas.tools.base import ToolError


def _thesis(db, ticker, *, status=ThesisStatus.approved, book=SWING_BOOK, conviction=None):
    run = PipelineRun(kind=RunKind.committee, ticker=ticker)
    db.add(run)
    db.flush()
    art = ResearchArtifact(
        run_id=run.id, ticker=ticker, agent_slug="investor", artifact_type="verdict", content={}
    )
    db.add(art)
    db.flush()
    if conviction is not None:
        db.add(
            ResearchArtifact(
                run_id=run.id,
                ticker=ticker,
                agent_slug="bull",
                artifact_type=ArtifactType.bull_case,
                content={"conviction": conviction, "expected_move_pct": 10},
            )
        )
    th = TradeThesis(
        run_id=run.id,
        verdict_artifact_id=art.id,
        ticker=ticker,
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=98.0,
        entry_zone_high=102.0,
        stop_price=95.0,
        targets=[110.0],
        status=status,
        book=book,
    )
    db.add(th)
    db.flush()
    return th


def _open_position(db, ticker, *, mark, qty=1.5, avg_price=100.0, opened_days_ago=1):
    th = _thesis(db, ticker, status=ThesisStatus.executed)
    p = Position(
        thesis_id=th.id,
        ticker=ticker,
        symbol=ticker,
        instrument=Instrument.shares,
        qty=qty,
        avg_price=avg_price,
        stop_price=95.0,
        last_mark_price=mark,
        opened_at=utcnow() - timedelta(days=opened_days_ago),
    )
    db.add(p)
    db.flush()
    return p


def _quote(monkeypatch, price):
    import midas.tools.market_data as md

    class _Q:
        data = {"price": price, "sector": "Tech"}

    monkeypatch.setattr(md, "get_quote", lambda t: _Q())
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: price)
    monkeypatch.setattr(monitor, "_underlying_price", lambda db, p: price)


def _recorder(calls, label, result=None):
    def _fn(*args, **kwargs):
        calls.append(label)
        return result

    return _fn


def _no_llm_work(monkeypatch, calls):
    """Every LLM-driven stage becomes a recorder."""
    import midas.pipeline.events as events

    monkeypatch.setattr(monitor, "maybe_auto_cycle", _recorder(calls, "auto_cycle"))
    monkeypatch.setattr(events, "maybe_event_scan", _recorder(calls, "event_scan"))
    monkeypatch.setattr(monitor, "maybe_portfolio_review", _recorder(calls, "portfolio_review"))
    monkeypatch.setattr(monitor, "reevaluate_position", _recorder(calls, "reevaluate", "hold"))


@pytest.fixture(autouse=True)
def _l2_small_account(monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 2)
    monkeypatch.setattr(settings, "fractional_shares", True)
    monkeypatch.setattr(settings, "min_swing_notional_usd", 25.0)
    monkeypatch.setattr(settings, "regime_filter_enabled", False)
    monkeypatch.setattr(settings, "core_enabled", False)
    monkeypatch.setattr(settings, "auto_cycle_weekdays", "")
    monkeypatch.setattr(settings, "event_scan_weekdays", "")


# ---- R4 ----


def test_one_failing_entry_does_not_roll_back_another_fill(db, monkeypatch):
    import midas.tools.market_data as md

    paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
    _thesis(db, "GOOD", conviction=9.0)
    _thesis(db, "BAD", conviction=3.0)
    db.commit()

    class _Q:
        data = {"price": 100.0, "sector": "Tech"}

    monkeypatch.setattr(md, "get_quote", lambda t: _Q())

    def _mark(db, order):
        if order.symbol == "BAD":
            raise ToolError("no market data source available for BAD")
        return 100.0

    monkeypatch.setattr(paper_broker, "mark_price", _mark)
    assert monitor.execute_approved_theses(db) == 1
    db.rollback()  # nothing uncommitted may be load-bearing
    good = db.query(TradeThesis).filter_by(ticker="GOOD").one()
    bad = db.query(TradeThesis).filter_by(ticker="BAD").one()
    assert good.status == ThesisStatus.executed
    assert bad.status == ThesisStatus.approved  # retried next pass
    assert db.query(Position).filter_by(ticker="GOOD", status=PositionStatus.open).count() == 1
    assert db.query(Order).filter_by(symbol="BAD").count() == 0


# ---- R5 ----


def test_halt_keeps_risk_management_running(factory, demo_router, monkeypatch):
    calls: list[str] = []
    _no_llm_work(monkeypatch, calls)
    monkeypatch.setattr(monitor, "execute_approved_theses", _recorder(calls, "entries", 0))
    monkeypatch.setattr(monitor, "execute_stop_exit", _recorder(calls, "stop_exit", "exit"))
    for name, label in (
        ("ratchet_stops", "ratchet"),
        ("apply_time_stops", "time_stops"),
    ):
        real = getattr(monitor, name)
        monkeypatch.setattr(monitor, name, lambda db, _r=real, _l=label: calls.append(_l) or _r(db))
    real_sync = paper_broker.sync_standing_stops
    monkeypatch.setattr(
        paper_broker,
        "sync_standing_stops",
        lambda db: calls.append("standing_stops") or real_sync(db),
    )
    with factory() as db:
        paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
        _open_position(db, "BRK", mark=90.0)  # below its 95 stop
        risk_engine.set_halt(db, "daily drawdown limit hit (test)")
        db.commit()
    _quote(monkeypatch, 90.0)
    summary = monitor.run_monitor_once(factory, demo_router)
    assert summary["halted"]
    assert {"stop_exit", "time_stops", "ratchet", "standing_stops"} <= set(calls)
    assert not {"entries", "auto_cycle", "event_scan", "portfolio_review", "reevaluate"} & set(
        calls
    )
    assert [r["trigger"] for r in summary["reviewed"]] == ["invalidation"]
    with factory() as db:
        assert db.get(SystemState, "monitor_heartbeat") is not None


# ---- R6 ----


def test_stop_breach_exits_run_before_llm_work(factory, demo_router, monkeypatch):
    calls: list[str] = []
    _no_llm_work(monkeypatch, calls)
    monkeypatch.setattr(monitor, "execute_approved_theses", _recorder(calls, "entries", 0))
    monkeypatch.setattr(monitor, "execute_stop_exit", _recorder(calls, "stop_exit", "exit"))
    with factory() as db:
        paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
        _open_position(db, "BRK", mark=90.0)
        db.commit()
    _quote(monkeypatch, 90.0)
    summary = monitor.run_monitor_once(factory, demo_router)
    assert summary["halted"] is None
    assert calls.index("stop_exit") < calls.index("auto_cycle")
    assert calls.index("stop_exit") < calls.index("event_scan")
    assert "portfolio_review" in calls
    assert [r["trigger"] for r in summary["reviewed"]] == ["invalidation"]


# ---- R7 ----


def test_entry_queue_serves_only_the_swing_book(db, monkeypatch):
    executed: list[str] = []

    class _Filled:
        status = OrderStatus.filled

    def _fake(db, th, approved_by):
        executed.append(th.ticker)
        return _Filled()

    monkeypatch.setattr(monitor.executor, "execute_thesis", _fake)
    _thesis(db, "SWING")
    _thesis(db, "SPY", book=CORE_BOOK)  # the core book's standing thesis
    assert monitor.execute_approved_theses(db) == 1
    assert executed == ["SWING"]


# ---- R9 ----


def _verdict(db, ticker, *, verdict, lo=None, hi=None, days_ago=0):
    run = PipelineRun(kind=RunKind.committee, ticker=ticker, status=RunStatus.done)
    db.add(run)
    db.flush()
    art = ResearchArtifact(
        run_id=run.id,
        ticker=ticker,
        agent_slug="investor",
        artifact_type=ArtifactType.verdict,
        content={
            "ticker": ticker,
            "verdict": verdict,
            "entry_zone_low": lo,
            "entry_zone_high": hi,
            "thesis_summary": "t",
            "no_trade_reason": "r",
        },
    )
    db.add(art)
    db.flush()
    if days_ago:
        art.created_at = utcnow() - timedelta(days=days_ago)
        db.flush()
    return art


def _cycle_setup(monkeypatch, candidates, ran):
    import midas.pipeline.committee as committee
    import midas.pipeline.screen as screen

    monkeypatch.setattr(settings, "auto_cycle_weekdays", "mon,tue,wed,thu,fri,sat,sun")
    monkeypatch.setattr(screen, "run_screen", lambda f, r: {"candidates": candidates})

    def _fake_committee(f, r, ticker):
        ran.append(ticker)
        return SimpleNamespace(status=RunStatus.done)

    monkeypatch.setattr(committee, "run_committee", _fake_committee)


def test_auto_cycle_cooldown_skips_recent_no_trade(factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "auto_cycle_max_committees", 2)
    monkeypatch.setattr(settings, "committee_cooldown_days", 7)
    candidates = [
        {"ticker": "COOL", "score": 90, "price": 100.0},  # no_trade 2d ago, price out of zone
        {"ticker": "INZONE", "score": 80, "price": 50.0},  # no_trade, but price inside its zone
        {"ticker": "FLAGGED", "score": 70, "price": 100.0},  # no_trade, but a new flag since
        {"ticker": "OLD", "score": 60, "price": 100.0},  # no_trade 10d ago: window passed
        {"ticker": "FRESH", "score": 50, "price": 100.0},  # never researched
    ]
    ran: list[str] = []
    _cycle_setup(monkeypatch, candidates, ran)
    with factory() as db:
        _verdict(db, "COOL", verdict="no_trade", lo=80.0, hi=90.0, days_ago=2)
        _verdict(db, "INZONE", verdict="no_trade", lo=45.0, hi=55.0, days_ago=2)
        _verdict(db, "FLAGGED", verdict="no_trade", lo=80.0, hi=90.0, days_ago=2)
        db.add(
            EventFlag(
                ticker="FLAGGED",
                kind="guidance_raise",
                severity="high",
                direction="positive",
                summary="raised",
                source_url="https://example.test",
            )
        )
        _verdict(db, "OLD", verdict="no_trade", lo=80.0, hi=90.0, days_ago=10)
        db.commit()
    out = monitor.maybe_auto_cycle(factory, demo_router)
    results = out["committees"]
    assert results["COOL"].startswith("skipped (cooldown: no_trade on ")
    assert ran == ["INZONE", "FLAGGED"]  # the cap counts committees actually run
    assert results["OLD"] == "skipped (committee cap)"
    assert results["FRESH"] == "skipped (committee cap)"


def test_auto_cycle_cooldown_can_be_disabled(factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "auto_cycle_max_committees", 4)
    monkeypatch.setattr(settings, "committee_cooldown_days", 0)
    ran: list[str] = []
    _cycle_setup(monkeypatch, [{"ticker": "COOL", "score": 90, "price": 100.0}], ran)
    with factory() as db:
        _verdict(db, "COOL", verdict="no_trade", lo=80.0, hi=90.0, days_ago=1)
        db.commit()
    out = monitor.maybe_auto_cycle(factory, demo_router)
    assert ran == ["COOL"]
    assert out["committees"]["COOL"] == "done"
