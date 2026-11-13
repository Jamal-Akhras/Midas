"""2026-11-13: entry rules the two November losses asked for.

NVDA was bought six days before earnings with the event as the thesis and
full size; AXP's stop and target were round numbers with no structure
behind them. Now a stop inside one ATR of the entry is rejected (noise,
not a broken thesis), an entry inside the earnings window needs the
verdict to declare it an earnings play and is sized at half risk (in the
committee's provisional check and at execution), and the daily cycle
waits out the first hour of the session."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from midas.agents.schemas import VerdictOutput
from midas.config import settings
from midas.execution import executor, paper_broker
from midas.models import (
    ArtifactType,
    AuditEvent,
    Instrument,
    ResearchArtifact,
    RunStatus,
    TradeThesis,
)
from midas.models.domain import Direction, PipelineRun, RunKind, ThesisStatus
from midas.pipeline import earnings
from midas.pipeline.committee import run_committee
from midas.risk import engine, verdict_checks
from midas.scheduler import monitor


def _verdict(**kw) -> VerdictOutput:
    base = dict(
        ticker="AXP",
        verdict="trade",
        direction="bullish",
        instrument="shares",
        entry_zone_low=320.0,
        entry_zone_high=323.0,
        stop_price=312.0,
        targets=[340.0],
        thesis_summary="t",
        what_invalidates_this=["a close under the stop"],
    )
    return VerdictOutput(**{**base, **kw})


# ---------- the stop against the ATR ----------


def test_a_stop_inside_one_atr_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "min_stop_atr", 1.0)
    v = _verdict(stop_price=315.0)  # 8 under the worst fill of 323
    (problem,) = verdict_checks.problems(v, 322.0, atr=12.0)
    assert problem == (
        "stop 315 is 0.67 ATR from the entry 323 (14-day ATR 12; minimum 1 ATR) - a stop "
        "inside one day's range is hit by noise, not by a broken thesis; put it under the "
        "swing low in the technical block"
    )
    assert verdict_checks.problems(v, 322.0, atr=6.0) == []
    assert verdict_checks.problems(v, 322.0) == []  # no ATR on record: not checked
    monkeypatch.setattr(settings, "min_stop_atr", 0.0)
    assert verdict_checks.problems(v, 322.0, atr=12.0) == []


def test_the_atr_rule_applies_to_an_option_verdicts_underlying_stop(monkeypatch):
    monkeypatch.setattr(settings, "min_stop_atr", 1.0)
    v = _verdict(instrument="option", stop_price=315.0)
    assert len(verdict_checks.problems(v, 322.0, atr=12.0)) == 1


# ---------- the earnings window ----------


def test_an_entry_inside_the_earnings_window_needs_an_earnings_play(monkeypatch):
    monkeypatch.setattr(settings, "entry_earnings_blackout_days", 7)
    v = _verdict()
    (problem,) = verdict_checks.problems(v, 322.0, earnings=(5, "2026-11-18"))
    assert problem == (
        "earnings on 2026-11-18 is 5 day(s) away, inside the 7-day entry window: rule "
        "no_trade, or set earnings_play true to hold through the event at half size and "
        "say so in the thesis"
    )
    ok = _verdict(earnings_play=True)
    assert verdict_checks.problems(ok, 322.0, earnings=(5, "2026-11-18")) == []
    assert verdict_checks.problems(v, 322.0, earnings=(9, "2026-11-22")) == []
    assert verdict_checks.problems(v, 322.0, earnings=(-1, "2026-11-12")) == []  # reported
    monkeypatch.setattr(settings, "entry_earnings_blackout_days", 0)
    assert verdict_checks.problems(v, 322.0, earnings=(5, "2026-11-18")) == []


def test_the_verdict_schema_carries_the_flag():
    assert _verdict().earnings_play is False
    assert _verdict(earnings_play=True).earnings_play is True


def test_earnings_days_are_read_from_the_runs_own_artifacts(db):
    run = PipelineRun(kind=RunKind.committee, ticker="AXP")
    db.add(run)
    db.flush()
    today = date(2026, 11, 13)
    assert earnings.days_away(db, run.id, today=today) is None
    db.add(
        ResearchArtifact(
            run_id=run.id,
            ticker="AXP",
            agent_slug="fundamentals",
            artifact_type=ArtifactType.fundamentals,
            content={"next_earnings_date": "January 25, 2027"},
        )
    )
    db.flush()
    assert earnings.days_away(db, run.id, today=today) == (73, "2027-01-25")
    db.add(
        ResearchArtifact(
            run_id=run.id,
            ticker="AXP",
            agent_slug="catalyst",
            artifact_type=ArtifactType.catalysts,
            content={"upcoming_earnings_date": "2026-11-18"},
        )
    )
    db.flush()
    assert earnings.days_away(db, run.id, today=today) == (5, "2026-11-18")  # catalysts first


def test_parse_handles_the_dates_the_agents_write():
    assert earnings.parse_date("2026-11-18") == date(2026, 11, 18)
    assert earnings.parse_date("January 25, 2027") == date(2027, 1, 25)
    assert earnings.parse_date("Nov 17, 2026") == date(2026, 11, 17)
    assert earnings.parse_date("") is None and earnings.parse_date("soon") is None


def test_half_risk_inside_the_earnings_window(db):
    full = engine.check_shares_trade(db, account_equity=1000.0, entry_price=100.0, stop_price=90.0)
    half = engine.check_shares_trade(
        db, account_equity=1000.0, entry_price=100.0, stop_price=90.0, risk_scale=0.5
    )
    assert full.max_qty == 1.0 and half.max_qty == 0.5
    assert full.checks["per_trade_risk"]["value"] == "1.0% -> 1.0 shares"
    assert half.checks["per_trade_risk"]["value"] == "1.0% x 0.5 -> 0.5 shares"
    assert half.checks["per_trade_risk"]["note"] == "half size: earnings inside the entry window"


def test_committee_refuses_a_verdict_inside_the_earnings_window(factory, demo_router, monkeypatch):
    from midas.providers import demo_provider

    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)
    monkeypatch.setattr(settings, "entry_earnings_blackout_days", 7)
    calls, fundamentals = demo_provider.SCRIPTS["fundamentals"]
    soon = (date.today() + timedelta(days=5)).isoformat()
    monkeypatch.setitem(
        demo_provider.SCRIPTS,
        "fundamentals",
        (calls, lambda t: {**fundamentals(t), "next_earnings_date": soon}),
    )
    result = run_committee(factory, demo_router, "ACME")
    assert result.status == RunStatus.done and result.thesis_id is None
    with factory() as db:
        events = list(
            db.scalars(select(AuditEvent).where(AuditEvent.event_type == "verdict.rejected_sanity"))
        )
        assert len(events) == 2
        assert any(
            "earnings on" in p and "earnings_play" in p for p in events[0].payload["problems"]
        )
        risk = db.scalar(
            select(ResearchArtifact).where(
                ResearchArtifact.artifact_type == ArtifactType.risk_check
            )
        )
        checks = risk.content["checks"]
        sizing = checks.get("per_trade_risk") or checks["premium_caps"]  # shares or the option
        assert sizing["note"] == "half size: earnings inside the entry window"
        assert "earnings" in risk.content["direction_note"]
        assert db.scalars(select(TradeThesis)).all() == []


def _thesis(db, ticker, *, lo, hi, stop, earnings_in_days):
    run = PipelineRun(kind=RunKind.committee, ticker=ticker)
    db.add(run)
    db.flush()
    art = ResearchArtifact(
        run_id=run.id, ticker=ticker, agent_slug="investor", artifact_type="verdict", content={}
    )
    db.add(art)
    db.add(
        ResearchArtifact(
            run_id=run.id,
            ticker=ticker,
            agent_slug="catalyst",
            artifact_type=ArtifactType.catalysts,
            content={
                "upcoming_earnings_date": (
                    date.today() + timedelta(days=earnings_in_days)
                ).isoformat()
            },
        )
    )
    db.flush()
    th = TradeThesis(
        run_id=run.id,
        verdict_artifact_id=art.id,
        ticker=ticker,
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=lo,
        entry_zone_high=hi,
        stop_price=stop,
        targets=[hi * 1.1],
        status=ThesisStatus.approved,
    )
    db.add(th)
    db.flush()
    return th


def _quote(monkeypatch, price):
    import midas.tools.market_data as md

    class _Q:
        data = {"price": price, "sector": "Financial"}

    monkeypatch.setattr(md, "get_quote", lambda t: _Q())
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: price)


@pytest.mark.parametrize("days,qty", [(3, 0.5), (20, 1.0)])
def test_executor_halves_the_size_inside_the_earnings_window(db, monkeypatch, days, qty):
    monkeypatch.setattr(settings, "autonomy_level", 2)
    monkeypatch.setattr(settings, "fractional_shares", True)
    monkeypatch.setattr(settings, "min_swing_notional_usd", 25.0)
    monkeypatch.setattr(settings, "regime_filter_enabled", False)
    monkeypatch.setattr(settings, "entry_earnings_blackout_days", 7)
    paper_broker.set_account(db, cash=1000.0, equity=1000.0)
    th = _thesis(db, "AXP", lo=100.0, hi=103.0, stop=93.0, earnings_in_days=days)
    _quote(monkeypatch, 102.0)
    order = executor.execute_thesis(db, th, approved_by="test")
    assert order.qty == pytest.approx(qty)  # $10 risk / $10 per share = 1 share at full risk


# ---------- the cycle waits out the first hour ----------


def test_the_cycle_waits_for_the_first_hour(factory, demo_router, monkeypatch):
    import midas.pipeline.screen as screen

    monkeypatch.setattr(settings, "auto_cycle_after_open_minutes", 60)
    monkeypatch.setattr(settings, "auto_cycle_weekdays", "mon,tue,wed,thu,fri,sat,sun")
    calls: list[str] = []
    monkeypatch.setattr(
        screen, "run_screen", lambda f, r: calls.append("screen") or {"candidates": []}
    )
    monkeypatch.setattr(monitor, "_minutes_since_open", lambda: 20.0)
    assert monitor.maybe_auto_cycle(factory, demo_router) is None
    assert calls == []
    monkeypatch.setattr(monitor, "_minutes_since_open", lambda: 75.0)
    assert monitor.maybe_auto_cycle(factory, demo_router) is not None
    assert calls == ["screen"]
