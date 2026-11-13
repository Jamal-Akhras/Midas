"""2026-11-13: the screen hands the screener the chart facts and keeps the
code's setup label.

On 2026-11-12 the screener rubber-stamped the deep scan's fifteen names
with one label and one paragraph. Now the task text carries a code-computed
facts line per ticker, each candidate's `setup` is the code's label (the
model's own goes to `screener_setup`), and the gate drops a pullback that
has not been reclaimed, saying so."""

from datetime import date

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.models import CostEvent, ModelCall, SystemState
from midas.models.base import utcnow
from midas.pipeline import screen
from midas.tools import demo_data
from tests.test_setup_facts import pullback_bars


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)
    monkeypatch.setattr(settings, "setup_gate_enabled", True)
    monkeypatch.setattr(settings, "setup_gate_require_reclaim", True)


def _screener_prompts(db) -> list[str]:
    rows = db.execute(
        select(ModelCall.user_prompt)
        .join(CostEvent, ModelCall.cost_event_id == CostEvent.id)
        .where(CostEvent.agent_slug == "screener")
    ).all()
    return [r[0] for r in rows]


def _scan(factory, kept):
    with factory() as db:
        db.add(
            SystemState(
                key="deep_scan",
                value={"date": utcnow().date().isoformat(), "kept": kept},
            )
        )
        db.commit()


def test_screener_is_shown_the_facts_and_candidates_carry_the_code_label(factory, demo_router):
    _scan(factory, [{"ticker": "ACME", "score": 88.4, "price": 184.2}])
    content = screen.run_screen(factory, demo_router)
    with factory() as db:
        prompts = _screener_prompts(db)
    assert prompts
    for prompt in prompts:
        assert "Chart facts (code-computed from completed bars" in prompt
        assert "ACME 88: pullback-in-uptrend;" in prompt  # scored name: score then facts
        assert "DUNE: pullback-in-uptrend;" in prompt  # universe name: facts, no score
        assert "RS20 vs SPY" in prompt
    acme = next(c for c in content["candidates"] if c["ticker"] == "ACME")
    assert acme["setup"] == "pullback-in-uptrend"
    assert acme["screener_setup"] == "breakout-pullback"
    assert acme["setup_note"].startswith("1.2% off the 20-day high in an uptrend; reclaimed")
    assert acme["score"] == 82  # the screener's ranking is its own


def test_screen_drops_a_falling_pullback_and_says_why(factory, demo_router, monkeypatch):
    real = demo_data.daily_bars

    def _bars(ticker, days=60):
        if ticker == "ACME":
            return pullback_bars(today=date.today())
        return real(ticker, days)

    monkeypatch.setattr(demo_data, "daily_bars", _bars)
    content = screen.run_screen(factory, demo_router)
    assert [c["ticker"] for c in content["candidates"]] == ["DUNE"]
    (gated,) = content["setup_rejections"]
    assert gated["ticker"] == "ACME" and gated["setup"] == "pullback-in-uptrend"
    assert gated["reasons"] == [
        "no reclaim yet: close 122 is under its 10-day mean 125 - the pullback is still "
        "falling; a setup needs a close above the 10-day and the prior close"
    ]
    assert gated["technical"]["swing_low"]["price"] == 121.0


def test_screen_keeps_the_falling_pullback_when_the_trigger_is_off(
    factory, demo_router, monkeypatch
):
    real = demo_data.daily_bars
    monkeypatch.setattr(
        demo_data,
        "daily_bars",
        lambda t, days=60: pullback_bars(today=date.today()) if t == "ACME" else real(t, days),
    )
    monkeypatch.setattr(settings, "setup_gate_require_reclaim", False)
    content = screen.run_screen(factory, demo_router)
    assert [c["ticker"] for c in content["candidates"]] == ["ACME", "DUNE"]


def test_a_universe_name_whose_bars_fail_keeps_the_screener_label(
    factory, demo_router, monkeypatch
):
    real = demo_data.daily_bars

    def _bars(ticker, days=60):
        if ticker == "ACME":
            raise RuntimeError("feed down")
        return real(ticker, days)

    monkeypatch.setattr(demo_data, "daily_bars", _bars)
    content = screen.run_screen(factory, demo_router)
    acme = next(c for c in content["candidates"] if c["ticker"] == "ACME")
    assert acme["setup"] == "breakout-pullback" and "screener_setup" not in acme
    with factory() as db:
        (prompt, *_) = _screener_prompts(db)
    assert "ACME: technical state unknown" in prompt
