"""2026-11-12: the premarket gap review.

NVDA was flagged at 15:02 local, "review scheduled for the open"; at the
open the stop layer sold first and the review never ran. Now the flag runs
the reevaluator in the same premarket pass, with news and the gap facts.
Its reach is one-directional: exit at the open, or hold under the
opening guard, or raise the stop; it cannot lower the stop or move the
disaster floor."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from midas.agents.defs import PROMPTS_DIR
from midas.agents.schemas import ReevaluationOutput
from midas.config import settings
from midas.execution import paper_broker
from midas.models import (
    SWING_BOOK,
    ArtifactType,
    AuditEvent,
    Instrument,
    Order,
    Position,
    PositionStatus,
    ResearchArtifact,
    SystemState,
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
    monkeypatch.setattr(settings, "premarket_gap_alert_pct", 3.0)
    monkeypatch.setattr(settings, "stop_disaster_multiple", 1.5)
    monkeypatch.setattr(settings, "stop_opening_guard_minutes", 30)
    monkeypatch.setattr(monitor, "maybe_portfolio_review", lambda f, r: None)
    # a closed position's post-mortem reads daily bars: keep that off the network
    import midas.tools.market_data as md
    from midas.tools.base import ToolResult

    monkeypatch.setattr(
        md, "get_daily_bars", lambda ticker, days=60: ToolResult(data=[], source="t")
    )


def _quote(monkeypatch, *, price, prev_close):
    import midas.tools.market_data as md
    from midas.tools.base import ToolResult
    from midas.tools.registry import TOOLS

    data = {"price": price, "prev_close": prev_close, "sector": "Semiconductors"}

    class _Q:
        pass

    _Q.data = data  # type: ignore[attr-defined]
    monkeypatch.setattr(md, "get_quote", lambda t: _Q())
    # the premarket sweep reads the quote through the tool registry
    monkeypatch.setattr(TOOLS["get_quote"], "fn", lambda ticker: ToolResult(data=data, source="t"))
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: price)
    monkeypatch.setattr(monitor, "_underlying_price", lambda db, p: price)


def _premarket(monkeypatch):
    monkeypatch.setattr(monitor, "market_session_open", lambda: False)
    monkeypatch.setattr(monitor, "_premarket_now", lambda: True)


def _session(monkeypatch, minutes_since_open=4.0):
    monkeypatch.setattr(monitor, "market_session_open", lambda: True)
    monkeypatch.setattr(monitor, "_premarket_now", lambda: False)
    monkeypatch.setattr(monitor, "_minutes_since_open", lambda: minutes_since_open)


def _book(factory, ticker="NVDA"):
    with factory() as db:
        paper_broker.set_account(db, cash=750.0, equity=989.0)
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
            invalidators=["price below 159.43"],
        )
        db.add(th)
        db.flush()
        p = Position(
            thesis_id=th.id,
            ticker=ticker,
            symbol=ticker,
            instrument=Instrument.shares,
            qty=0.792,
            avg_price=172.116,
            stop_price=159.43,
            last_mark_price=173.73,
            opened_at=utcnow() - timedelta(days=1),
            next_review_at=utcnow() + timedelta(days=1),
        )
        db.add(p)
        db.commit()
        return p.id


def _reviewer(decision="hold", *, new_stop=None, reasons=None):
    answer = json.dumps(
        {
            "ticker": "NVDA",
            "decision": decision,
            "reasons": reasons or ["overnight gap on a headline; stop intact"],
            "thesis_still_valid": decision == "hold",
            "time_pressure_note": "",
            "next_review_days": 1,
            "new_stop": new_stop,
        }
    )
    provider = FakeProvider("openai", ["read the news", answer])
    return provider, ModelRouter(providers={"openai": provider})


def _state(db, key) -> dict:
    row = db.get(SystemState, key)
    return dict(row.value) if row else {}


def _events(db, kind) -> list[str]:
    return [e.summary for e in db.scalars(select(AuditEvent).where(AuditEvent.event_type == kind))]


# ---------- the review runs premarket, with the facts ----------


def test_a_gap_runs_the_review_in_the_same_premarket_pass(factory, monkeypatch):
    _book(factory)
    _premarket(monkeypatch)
    _quote(monkeypatch, price=163.6, prev_close=173.73)  # -5.8%
    provider, router = _reviewer("hold")
    summary = monitor.run_monitor_once(factory, router)
    assert summary["premarket_flags"] == ["NVDA"]
    assert summary["reviewed"] == [
        {"position_id": summary["reviewed"][0]["position_id"], "trigger": "gap", "decision": "hold"}
    ]
    prompt = str(provider.calls[0]["messages"][-1]["content"])
    assert "premarket gap -5.8%" in prompt
    assert "disaster floor 153.09" in prompt and "stop 159.43" in prompt
    assert "PREMARKET GAP REVIEW" in prompt and "30-minute opening guard" in prompt
    with factory() as db:
        assert _events(db, "premarket.gap_flagged") == [
            "NVDA: premarket -5.8% vs prev close (173.73 -> 163.6) - gap review now"
        ]
        assert _state(db, "open_exits") == {}  # hold parks nothing
        assert db.scalars(select(Order)).all() == []
    # a second premarket pass does not review the same gap again
    monitor.run_monitor_once(factory, router)
    assert len(provider.calls) == 2  # the tool-loop reply and the answer, once


def test_exit_waits_for_the_open_and_then_goes_first(factory, monkeypatch):
    pid = _book(factory)
    _premarket(monkeypatch)
    _quote(monkeypatch, price=163.6, prev_close=173.73)
    _, router = _reviewer("exit", reasons=["guidance cut on the wire overnight"])
    summary = monitor.run_monitor_once(factory, router)
    assert summary["reviewed"][0]["decision"] == "exit"
    with factory() as db:
        assert db.scalars(select(Order)).all() == []  # nothing fills premarket
        assert db.get(Position, pid).status == PositionStatus.open
        parked = _state(db, "open_exits")
        assert parked[str(pid)]["decision"] == "exit"
        assert _events(db, "position.exit_at_open") == [
            "NVDA: gap review says exit - executed on the first pass after the open, "
            "before anything else"
        ]
    # the open: four minutes in, price under the stop but above the floor -
    # the guard would hold a plain breach; the parked exit does not wait
    _session(monkeypatch, minutes_since_open=4.0)
    _quote(monkeypatch, price=155.6, prev_close=173.73)
    summary = monitor.run_monitor_once(factory, router)
    assert summary["open_exits"] == ["NVDA"]
    with factory() as db:
        p = db.get(Position, pid)
        assert p.status == PositionStatus.closed
        (sell,) = db.scalars(select(Order)).all()
        assert sell.filled_price == 155.6
        assert _state(db, "open_exits") == {}


def test_a_higher_stop_is_accepted(factory, monkeypatch):
    pid = _book(factory)
    _premarket(monkeypatch)
    _quote(monkeypatch, price=163.6, prev_close=173.73)
    _, router = _reviewer("hold", new_stop=161.0)
    monitor.run_monitor_once(factory, router)
    with factory() as db:
        assert db.get(Position, pid).stop_price == 161.0
        assert _events(db, "stop.tightened") == ["NVDA: stop 159.43 -> 161.0 (model raised it)"]


@pytest.mark.parametrize("asked", [150.0, 170.0])
def test_a_lower_or_at_market_stop_is_refused(factory, monkeypatch, asked):
    pid = _book(factory)
    _premarket(monkeypatch)
    _quote(monkeypatch, price=163.6, prev_close=173.73)
    _, router = _reviewer("hold", new_stop=asked)
    monitor.run_monitor_once(factory, router)
    with factory() as db:
        assert db.get(Position, pid).stop_price == 159.43
        (refused,) = _events(db, "stop.change_refused")
        assert f"stop {asked}" in refused and "never cut" in refused


def test_a_small_gap_is_not_reviewed(factory, monkeypatch):
    _book(factory)
    _premarket(monkeypatch)
    _quote(monkeypatch, price=170.0, prev_close=173.73)  # -2.1%
    provider, router = _reviewer("hold")
    summary = monitor.run_monitor_once(factory, router)
    assert summary["premarket_flags"] == [] and provider.calls == []


def test_a_halt_skips_the_review_but_not_the_parked_exit(factory, monkeypatch):
    pid = _book(factory)
    with factory() as db:
        monitor._upsert_state(
            db,
            "open_exits",
            {str(pid): {"decision": "exit", "reasons": ["parked yesterday"], "at": "x"}},
        )
        db.commit()
    monkeypatch.setattr(monitor.risk_engine, "get_halt", lambda db: {"reason": "daily loss"})
    _session(monkeypatch, minutes_since_open=4.0)
    _quote(monkeypatch, price=160.0, prev_close=173.73)
    provider, router = _reviewer("hold")
    summary = monitor.run_monitor_once(factory, router)
    assert summary["halted"] and summary["open_exits"] == ["NVDA"]
    # no gap review ran under the halt (the post-mortem of the closed position may have)
    assert not any(
        "PREMARKET GAP REVIEW" in str(c["messages"][0]["content"]) for c in provider.calls
    )
    with factory() as db:
        assert db.get(Position, pid).status == PositionStatus.closed


def test_schema_and_prompt_carry_the_new_stop():
    out = ReevaluationOutput(
        ticker="NVDA", decision="hold", reasons=["r"], thesis_still_valid=True, new_stop=161.0
    )
    assert out.new_stop == 161.0
    text = (PROMPTS_DIR / "reevaluator.md").read_text(encoding="utf-8")
    assert "PREMARKET GAP REVIEW" in text and "new_stop" in text
