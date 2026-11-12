"""2026-11-12: the reward-to-risk floor.

AXP: entry 322.99, stop 303.53 (6.0% away), first target 335.1 (3.8%
away) - 0.6 to 1, and nothing in code objected. Now a shares verdict's
first target must be at least MIN_REWARD_TO_RISK stop distances from the
entry (the sanity gate re-prompts once), and the executor re-checks it."""

import pytest
from sqlalchemy import select

from midas.agents.schemas import VerdictOutput
from midas.config import settings
from midas.execution import executor, paper_broker
from midas.models import (
    SWING_BOOK,
    ArtifactType,
    AuditEvent,
    Instrument,
    ResearchArtifact,
    ThesisStatus,
    TradeThesis,
)
from midas.models.domain import Direction, PipelineRun, RunKind
from midas.pipeline import committee
from midas.providers import demo_provider
from midas.risk import engine as risk_engine
from midas.risk import verdict_checks
from midas.scheduler import monitor


@pytest.fixture(autouse=True)
def _floor(monkeypatch):
    monkeypatch.setattr(settings, "min_reward_to_risk", 1.5)
    monkeypatch.setattr(settings, "regime_filter_enabled", False)
    monkeypatch.setattr(settings, "core_enabled", False)


def _verdict(**kw) -> VerdictOutput:
    base = dict(
        ticker="AXP",
        verdict="trade",
        direction="bullish",
        instrument="shares",
        entry_zone_low=322.9,
        entry_zone_high=322.9,
        stop_price=303.53,
        targets=[335.1],
        thesis_summary="post-earnings drift",
        what_invalidates_this=["a close below the stop"],
    )
    base.update(kw)
    return VerdictOutput(**base)


# ---------- the sanity gate ----------


def test_axp_levels_fail_the_floor():
    (problem,) = verdict_checks.problems(_verdict(), spot=322.99)
    assert "reward-to-risk is 0.63 to 1" in problem
    assert "minimum 1.5" in problem


def test_a_structural_stop_passes():
    # stop just under the 50-day at 315.9: risk 7.0, reward 12.2 -> 1.74
    assert verdict_checks.problems(_verdict(stop_price=315.9), spot=322.99) == []


def test_a_trade_without_a_target_cannot_be_checked_and_fails():
    (problem,) = verdict_checks.problems(_verdict(targets=[]), spot=322.99)
    assert "first target" in problem


def test_the_floor_is_measured_from_the_worst_accepted_fill():
    # zone 320-326, stop 310, target 338: from the zone top the risk is 16
    # and the reward 12 -> 0.75; a wide zone does not hide a thin trade
    (problem,) = verdict_checks.problems(
        _verdict(entry_zone_low=320.0, entry_zone_high=326.0, stop_price=310.0, targets=[338.0]),
        spot=323.0,
    )
    assert "0.75 to 1" in problem


def test_options_are_sized_by_premium_and_skip_the_floor():
    assert verdict_checks.problems(_verdict(instrument="option"), spot=322.99) == []


def test_floor_off_restores_the_old_gate(monkeypatch):
    monkeypatch.setattr(settings, "min_reward_to_risk", 0.0)
    assert verdict_checks.problems(_verdict(), spot=322.99) == []


# ---------- the risk engine and the executor ----------


def test_risk_engine_checks_the_first_target_when_given(db):
    paper_broker.set_account(db, cash=800.0, equity=989.0)
    rc = risk_engine.check_shares_trade(
        db, account_equity=989.0, entry_price=322.99, stop_price=303.53, first_target=335.1
    )
    assert rc.ok is False
    assert rc.checks["reward_to_risk"]["ok"] is False
    assert rc.checks["reward_to_risk"]["value"] == "0.62:1"  # from the 322.99 fill
    rc = risk_engine.check_shares_trade(
        db, account_equity=989.0, entry_price=322.99, stop_price=315.9, first_target=335.1
    )
    assert rc.ok is True and rc.checks["reward_to_risk"]["ok"] is True
    rc = risk_engine.check_shares_trade(
        db, account_equity=989.0, entry_price=322.99, stop_price=303.53
    )
    assert "reward_to_risk" not in rc.checks  # the committee's provisional sizing has no target


def test_executor_refuses_a_thin_thesis(db, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 2)
    monkeypatch.setattr(settings, "fractional_shares", True)
    import midas.tools.market_data as md

    class _Q:
        data = {"price": 322.9, "sector": "Financial Services"}

    monkeypatch.setattr(md, "get_quote", lambda t: _Q())
    paper_broker.set_account(db, cash=800.0, equity=989.0)
    run = PipelineRun(kind=RunKind.committee, ticker="AXP")
    db.add(run)
    db.flush()
    art = ResearchArtifact(
        run_id=run.id, ticker="AXP", agent_slug="investor", artifact_type="verdict", content={}
    )
    db.add(art)
    db.flush()
    th = TradeThesis(
        run_id=run.id,
        verdict_artifact_id=art.id,
        ticker="AXP",
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=322.9,
        entry_zone_high=322.9,
        stop_price=303.53,
        targets=[335.1],
        status=ThesisStatus.approved,
        book=SWING_BOOK,
    )
    db.add(th)
    db.flush()
    with pytest.raises(executor.ExecutionRefused, match="reward_to_risk"):
        executor.execute_thesis(db, th, approved_by="test")


# ---------- the committee re-prompts once ----------


def test_committee_reprompts_a_thin_verdict_and_keeps_the_fix(factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)
    calls, real_fn = demo_provider.SCRIPTS["investor"]
    answers = iter([303.53, 315.9])  # thin stop first, structural stop on the re-prompt

    def _shares_verdict(ticker: str) -> dict:
        price = demo_provider._quote(ticker)["price"]
        v = real_fn(ticker)
        v.update(
            instrument="shares",
            entry_zone_low=round(price, 2),
            entry_zone_high=round(price, 2),
            stop_price=round(price * next(answers) / 322.99, 2),
            targets=[round(price * 335.1 / 322.99, 2)],
        )
        return v

    monkeypatch.setitem(demo_provider.SCRIPTS, "investor", (calls, _shares_verdict))
    result = committee.run_committee(factory, demo_router, "ACME")
    assert result.thesis_id is not None
    with factory() as db:
        rejected = db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "verdict.rejected_sanity")
        ).all()
        assert len(rejected) == 1
        assert any("reward-to-risk" in p for p in rejected[0].payload["problems"])
        th = db.get(TradeThesis, result.thesis_id)
        price = demo_provider._quote("ACME")["price"]
        assert th.stop_price == round(price * 315.9 / 322.99, 2)
        verdicts = db.scalars(
            select(ResearchArtifact).where(ResearchArtifact.artifact_type == ArtifactType.verdict)
        ).all()
        assert len(verdicts) == 2  # the rejected one stays on record


def test_floor_is_part_of_the_strategy_fingerprint(monkeypatch):
    before = monitor._strategy_fingerprint()
    monkeypatch.setattr(settings, "min_reward_to_risk", 2.0)
    assert monitor._strategy_fingerprint() != before
