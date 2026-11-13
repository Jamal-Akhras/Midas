"""2026-11-11: the setup gate and the tie note.

The screener called NVDA - 25% off its high, below its 50-day, down 18% in
five sessions on doubled volume - a "breakout-pullback"; triage repeated the
label; the risk check told the investor the bull "won" a 7-7; the investor
went long. Every number was already in the technical block. Now the gate
checks the label against those numbers in code, at the screen (drop) and in
the screen context (a hard line for manual runs), and a tie is a tie."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.models import (
    ArtifactType,
    AuditEvent,
    CostEvent,
    ModelCall,
    ResearchArtifact,
    SystemState,
)
from midas.models import ScreenCandidate as ScreenRow
from midas.models.base import utcnow
from midas.pipeline import committee, screen
from midas.pipeline.setup_gate import setup_gate
from midas.scheduler import monitor
from midas.tools import demo_data

TODAY = date.today()

# the technical blocks the committee actually read on 2026-11-11 (ledger)
NVDA = {
    "close": 168.89,
    "ma10": 195.836,
    "ma20": 206.183,
    "ma50": 217.9978,
    "extension_vs_10d_pct": -13.76,
    "pullback_from_20d_high_pct": 25.27,
    "change_5d_pct": -18.29,
    "volume_5d_over_20d": 1.97,
    "above_ma50": False,
}
PFE = {
    "close": 29.36,
    "ma10": 29.924,
    "ma20": 29.8455,
    "extension_vs_10d_pct": -1.88,
    "pullback_from_20d_high_pct": 6.62,
    "change_5d_pct": -2.13,
    "volume_5d_over_20d": 1.26,
    "above_ma50": True,
}


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)
    monkeypatch.setattr(settings, "setup_gate_enabled", True)
    monkeypatch.setattr(settings, "setup_gate_max_pullback_pct", 12.0)
    monkeypatch.setattr(settings, "setup_gate_max_5d_drop_pct", 10.0)
    monkeypatch.setattr(settings, "setup_gate_max_volume_ratio", 1.3)


# ---------- the pure gate ----------


def test_nvda_fails_the_gate_on_every_count():
    failures = setup_gate(NVDA, "breakout-pullback")
    text = " | ".join(failures)
    assert len(failures) == 4
    assert "below its 50-day average" in text
    assert "25.3% off its 20-day high" in text
    assert "down 18.3% in five sessions" in text
    assert "volume 1.97x" in text


def test_pfe_passes_the_gate():
    assert setup_gate(PFE, "post-earnings-drift") == []


def test_volume_rule_applies_only_to_pullback_setups():
    block = {**PFE, "volume_5d_over_20d": 1.8}
    assert setup_gate(block, "post-earnings-drift") == []
    assert setup_gate(block, "fundamentals-momentum") == []
    assert len(setup_gate(block, "breakout-pullback")) == 1
    assert len(setup_gate(block, "Breakout_Pullback")) == 1  # the label's case is not evidence


def test_unknown_state_passes_and_the_gate_can_be_switched_off(monkeypatch):
    assert setup_gate(None, "breakout-pullback") == []
    assert setup_gate({"close": 10.0}, "breakout-pullback") == []  # no numbers, no objection
    monkeypatch.setattr(settings, "setup_gate_enabled", False)
    assert setup_gate(NVDA, "breakout-pullback") == []


def test_gate_thresholds_are_part_of_the_strategy_fingerprint(monkeypatch):
    before = monitor._strategy_fingerprint()
    monkeypatch.setattr(settings, "setup_gate_max_pullback_pct", 15.0)
    assert monitor._strategy_fingerprint() != before


# ---------- at the screen ----------


def _crash_bars(ticker: str, days: int = 60) -> list[dict]:
    """A steady uptrend that falls 25% over the last five sessions on
    expanding volume - the NVDA shape."""
    if ticker != "DUNE":
        return demo_data.daily_bars(ticker, days)
    bars = []
    peak = 100.0 + 0.5 * (days - 6)
    for i in range(days):
        d = TODAY - timedelta(days=days - i)
        if i < days - 5:
            close = 100.0 + 0.5 * i
            volume = 1_000
        else:
            close = peak * (1 - 0.05 * (i - (days - 6)))
            volume = 2_500
        bars.append(
            {
                "date": d.isoformat(),
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": round(close, 2),
                "volume": volume,
            }
        )
    return bars


def test_screen_drops_a_broken_chart_and_records_why(factory, demo_router, monkeypatch):
    monkeypatch.setattr(demo_data, "daily_bars", _crash_bars)
    content = screen.run_screen(factory, demo_router)
    assert [c["ticker"] for c in content["candidates"]] == ["ACME"]
    (gated,) = content["setup_rejections"]
    assert gated["ticker"] == "DUNE" and gated["setup"] == "none"  # the code's label
    assert gated["screener_setup"] == "post-earnings-drift"
    assert any("50-day" in r for r in gated["reasons"])
    assert any("five sessions" in r for r in gated["reasons"])
    assert gated["technical"]["above_ma50"] is False
    with factory() as db:
        rows = [r.ticker for r in db.scalars(select(ScreenRow))]
        assert rows == ["ACME"]  # the dropped name never becomes a candidate row
        ev = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "screen.gated"))
        assert ev is not None and ev.summary.startswith("DUNE: dropped by the setup gate")
        done = db.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == "run.completed")
            .order_by(AuditEvent.created_at.desc())
        )
        assert "gate-dropped 1" in done.summary and done.payload["gated"] == ["DUNE"]


def test_screen_keeps_everything_when_the_gate_is_off(factory, demo_router, monkeypatch):
    monkeypatch.setattr(demo_data, "daily_bars", _crash_bars)
    monkeypatch.setattr(settings, "setup_gate_enabled", False)
    content = screen.run_screen(factory, demo_router)
    assert [c["ticker"] for c in content["candidates"]] == ["ACME", "DUNE"]
    assert content["setup_rejections"] == []


def test_screen_keeps_a_candidate_whose_bars_are_unavailable(factory, demo_router, monkeypatch):
    def _no_bars(ticker, days=60):
        raise RuntimeError("feed down")

    monkeypatch.setattr(demo_data, "daily_bars", _no_bars)
    content = screen.run_screen(factory, demo_router)
    assert [c["ticker"] for c in content["candidates"]] == ["ACME", "DUNE"]


def _screener_prompts(db) -> list[str]:
    rows = db.execute(
        select(ModelCall.user_prompt)
        .join(CostEvent, ModelCall.cost_event_id == CostEvent.id)
        .where(CostEvent.agent_slug == "screener")
    ).all()
    return [r[0] for r in rows]


def test_screener_is_told_the_quant_scores_and_the_gate(factory, demo_router):
    with factory() as db:
        db.add(
            SystemState(
                key="deep_scan",
                value={
                    "date": utcnow().date().isoformat(),
                    "kept": [{"ticker": "NEWCO", "score": 88.4, "price": 42.0}],
                },
            )
        )
        db.commit()
    screen.run_screen(factory, demo_router)
    with factory() as db:
        prompts = _screener_prompts(db)  # one per model call; the task text is in each
    assert prompts
    for prompt in prompts:
        assert "NEWCO" in prompt
        assert "Deep-scan quant scores today" in prompt and "NEWCO 88" in prompt
        assert "Setup gate (deterministic, after you)" in prompt
        assert "more than 12% off its 20-day high" in prompt


def test_task_text_without_a_scan_or_gate_is_just_universe_and_floors(monkeypatch):
    monkeypatch.setattr(settings, "setup_gate_enabled", False)
    text = screen.screen_task_text(["AAA", "BBB"], [])
    assert text.startswith("Scan this universe for setup candidates. Universe: AAA, BBB")
    assert "Deep-scan" not in text and "Setup gate" not in text


# ---------- in the screen context (manual runs) ----------


def test_screen_context_carries_the_gate_verdict(factory, demo_router, monkeypatch):
    monkeypatch.setattr(demo_data, "daily_bars", _crash_bars)
    committee.run_committee(factory, demo_router, "DUNE")  # manual run: no screen record
    with factory() as db:
        ctx = db.scalar(
            select(ResearchArtifact).where(
                ResearchArtifact.artifact_type == ArtifactType.screen_context
            )
        ).content
        assert ctx["gate"] == {
            "passed": False,
            "failures": ctx["gate"]["failures"],
            "evaluated": True,
        }
        assert ctx["gate"]["failures"] and ctx["gate_note"].startswith("SETUP GATE: FAILED - ")
        prompts = db.execute(
            select(ModelCall.user_prompt)
            .join(CostEvent, ModelCall.cost_event_id == CostEvent.id)
            .where(CostEvent.agent_slug == "triage")
        ).all()
        assert prompts and all("SETUP GATE: FAILED" in p[0] for p in prompts)


def test_screen_context_reports_a_passing_gate(factory, demo_router):
    committee.run_committee(factory, demo_router, "ACME")
    with factory() as db:
        ctx = db.scalar(
            select(ResearchArtifact).where(
                ResearchArtifact.artifact_type == ArtifactType.screen_context
            )
        ).content
    assert ctx["gate"] == {"passed": True, "failures": [], "evaluated": True}
    assert ctx["gate_note"].startswith("setup gate: passed")


def test_gate_is_not_evaluated_without_a_technical_block(db):
    ctx = committee.screen_context_content(db, "ACME", [], "daily bars unavailable")
    assert ctx["gate"] == {"passed": True, "failures": [], "evaluated": False}
    assert "not evaluated" in ctx["gate_note"]


# ---------- the tie note ----------


def test_a_conviction_tie_is_reported_as_a_tie():
    assert "tied at 7.0" in committee.conviction_comparison(7.0, 7.0)
    assert "won" not in committee.conviction_comparison(7.0, 7.0)
    assert committee.conviction_comparison(7.0, 5.0).startswith("bull case won")
    assert committee.conviction_comparison(5.0, 7.0).startswith("bear case won")
