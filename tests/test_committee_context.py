"""The committee sees the setup it was selected for and knows its horizon.

Before 2026-11-10 the committee received only a ticker: it judged 3-week
pullback trades on P/E and insider filings and refused 4/4. Now every run
stores a code-built `screen_context` artifact (today's screener record plus
a deterministic technical block from completed daily bars) and fences it
into triage, bull, bear and investor; every task text states the swing
horizon; and in the long-only book the shares fallback is sized as a LONG
even when the bear case won the conviction comparison."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.models import ArtifactType, CostEvent, ModelCall, ResearchArtifact, RunStatus
from midas.pipeline import committee
from midas.pipeline.committee import run_committee
from midas.pipeline.screen import run_screen
from midas.providers import demo_provider
from midas.risk import structurer_checks

TODAY = date.today()


def _bars(n: int, last_day: date, start: float = 100.0, step: float = 0.5) -> list[dict]:
    out = []
    for i in range(n):
        close = start + step * i
        out.append(
            {
                "date": (last_day - timedelta(days=n - 1 - i)).isoformat(),
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1_000 + (100 if i >= n - 5 else 0),
            }
        )
    return out


# ---------- deterministic technical block ----------


def test_technical_block_uses_completed_bars_only():
    bars = _bars(60, TODAY)  # the last bar is TODAY's partial session
    tech = committee.technical_block(bars, today=TODAY)
    assert tech is not None
    assert tech["as_of"] == (TODAY - timedelta(days=1)).isoformat()
    assert tech["bars_used"] == 59
    closes = [b["close"] for b in bars[:-1]]
    assert tech["close"] == closes[-1]
    assert tech["ma10"] == round(sum(closes[-10:]) / 10, 4)
    assert tech["ma20"] == round(sum(closes[-20:]) / 20, 4)
    assert tech["ma50"] == round(sum(closes[-50:]) / 50, 4)
    assert tech["extension_vs_10d_pct"] == round((closes[-1] / tech["ma10"] - 1) * 100, 2)
    high20 = max(b["high"] for b in bars[:-1][-20:])
    assert tech["pullback_from_20d_high_pct"] == round((1 - closes[-1] / high20) * 100, 2)
    assert tech["change_5d_pct"] == round((closes[-1] / closes[-6] - 1) * 100, 2)
    vols = [b["volume"] for b in bars[:-1]]
    assert tech["volume_5d_over_20d"] == round((sum(vols[-5:]) / 5) / (sum(vols[-20:]) / 20), 2)
    assert tech["above_ma50"] is True


def test_technical_block_needs_enough_history():
    assert committee.technical_block(_bars(5, TODAY - timedelta(days=1)), today=TODAY) is None
    assert committee.technical_block([], today=TODAY) is None


def test_technical_block_without_fifty_bars_reports_ma50_unknown():
    tech = committee.technical_block(_bars(25, TODAY - timedelta(days=1)), today=TODAY)
    assert tech is not None and tech["ma50"] is None and tech["above_ma50"] is None


# ---------- end to end in demo mode ----------


@pytest.fixture()
def demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)


def _prompts(db, slug: str) -> list[str]:
    rows = db.execute(
        select(ModelCall.user_prompt)
        .join(CostEvent, ModelCall.cost_event_id == CostEvent.id)
        .where(CostEvent.agent_slug == slug)
    ).all()
    return [r[0] for r in rows]


def _context(db) -> dict:
    art = db.scalar(
        select(ResearchArtifact).where(
            ResearchArtifact.artifact_type == ArtifactType.screen_context
        )
    )
    assert art is not None, "every committee run stores a screen_context artifact"
    assert art.model_used == "deterministic-code"
    return art.content


def test_screen_context_is_stored_and_fenced_into_the_committee(demo_mode, factory, demo_router):
    run_screen(factory, demo_router)  # today's screen lists ACME as breakout-pullback
    result = run_committee(factory, demo_router, "ACME")
    assert result.status == RunStatus.awaiting_approval

    with factory() as db:
        ctx = _context(db)
        assert ctx["screen"]["setup"] == "breakout-pullback"
        assert ctx["screen"]["score"] == 82
        assert ctx["screen"]["price_at_screen"] > 0
        assert "pulled back" in ctx["screen"]["rationale"]
        assert ctx["technical"] is not None and ctx["technical"]["bars_used"] >= 20
        for slug in ("triage", "bull", "bear", "investor"):
            prompts = _prompts(db, slug)
            assert prompts, slug
            assert all('<committee_document name="screen_context"' in p for p in prompts), slug
            assert all("breakout-pullback" in p for p in prompts), slug
        # the research stages never saw it (they research the company, not the chart)
        assert all('name="screen_context"' not in p for p in _prompts(db, "fundamentals"))


def test_manual_run_without_a_screen_still_has_the_technical_block(demo_mode, factory, demo_router):
    run_committee(factory, demo_router, "ACME")
    with factory() as db:
        ctx = _context(db)
        assert ctx["screen"] is None
        assert "no screen record" in ctx["screen_note"]
        assert ctx["technical"]["close"] > 0


def test_every_llm_stage_is_told_the_swing_horizon(demo_mode, factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "max_holding_days", 21)
    run_committee(factory, demo_router, "ACME")
    expected = (
        "SWING trade in a long-only book: entries are held 5-21 days with a stop 2-25% below entry"
    )
    with factory() as db:
        for slug in (
            "fundamentals",
            "catalyst",
            "triage",
            "deep-research",
            "bull",
            "bear",
            "investor",
        ):
            prompts = _prompts(db, slug)
            assert prompts and all(expected in p for p in prompts), slug


def test_bear_win_still_sizes_a_long_in_the_long_only_book(
    demo_mode, factory, demo_router, monkeypatch
):
    calls, _ = demo_provider.SCRIPTS["bear"]

    def strong_bear(ticker: str) -> dict:
        return {**demo_provider._bear(ticker), "conviction": 9.0, "expected_move_pct": -12.0}

    monkeypatch.setitem(demo_provider.SCRIPTS, "bear", (calls, strong_bear))
    # no option structure: the shares fallback is what gets sized
    monkeypatch.setattr(structurer_checks, "candidate_structures", lambda *a, **k: [])

    run_committee(factory, demo_router, "ACME")

    with factory() as db:
        risk = db.scalar(
            select(ResearchArtifact).where(
                ResearchArtifact.artifact_type == ArtifactType.risk_check
            )
        ).content
        assert risk["ok"] is True, risk["checks"]
        assert risk["max_qty"] > 0
        assert risk["checks"]["stop_below_entry"]["ok"] is True
        assert risk["sized_direction"] == "bullish"
        assert risk["direction_note"].startswith(
            "bear case won the conviction comparison (9.0 vs 7.5); a long was still sized at"
        )
        assert "for your judgment" in risk["direction_note"]
        investor_prompts = _prompts(db, "investor")
        assert investor_prompts and all("bear case won" in p for p in investor_prompts)
