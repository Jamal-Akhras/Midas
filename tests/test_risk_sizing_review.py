"""2026-11-10 review, step 1: sizing on a $1,000 account.

R1: a fraction of a share is a valid size, floored only by a minimum
notional. R8: the swing book's exposure budget yields to the core
allocation so the two books cannot both claim 60% of equity."""

from midas.config import settings
from midas.models import Instrument, Position, TradeThesis, utcnow
from midas.models.domain import Direction, PipelineRun, ResearchArtifact, RunKind, ThesisStatus
from midas.risk import engine


def _open_position(db, ticker, *, qty, avg_price, sector):
    run = PipelineRun(kind=RunKind.committee, ticker=ticker)
    db.add(run)
    db.flush()
    art = ResearchArtifact(
        run_id=run.id, ticker=ticker, agent_slug="investor", artifact_type="verdict", content={}
    )
    db.add(art)
    db.flush()
    thesis = TradeThesis(
        run_id=run.id,
        verdict_artifact_id=art.id,
        ticker=ticker,
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=avg_price - 1,
        entry_zone_high=avg_price + 1,
        stop_price=avg_price * 0.95,
        status=ThesisStatus.executed,
    )
    db.add(thesis)
    db.flush()
    p = Position(
        thesis_id=thesis.id,
        ticker=ticker,
        symbol=ticker,
        instrument=Instrument.shares,
        sector=sector,
        qty=qty,
        avg_price=avg_price,
        opened_at=utcnow(),
    )
    db.add(p)
    db.flush()
    return p


def test_fraction_of_a_share_is_a_valid_size(db, monkeypatch):
    """GOOGL on 2026-11-10: a $10 risk budget and a $150 notional cap on a
    $1,000 account size to about half a share. That is a trade, not dust."""
    monkeypatch.setattr(settings, "fractional_shares", True)
    monkeypatch.setattr(settings, "min_swing_notional_usd", 25.0)
    rc = engine.check_shares_trade(db, account_equity=1_000, entry_price=200.0, stop_price=190.0)
    assert rc.ok
    assert rc.max_qty == 0.75
    assert rc.checks["min_notional"]["ok"]


def test_min_notional_refuses_dust(db, monkeypatch):
    monkeypatch.setattr(settings, "fractional_shares", True)
    monkeypatch.setattr(settings, "min_swing_notional_usd", 25.0)
    # $1 risk budget on a $100 account against a $100-wide stop: 0.01 shares = $2
    rc = engine.check_shares_trade(db, account_equity=100, entry_price=200.0, stop_price=100.0)
    assert not rc.ok
    assert not rc.checks["min_notional"]["ok"]


def test_min_notional_can_be_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "fractional_shares", True)
    monkeypatch.setattr(settings, "min_swing_notional_usd", 0.0)
    rc = engine.check_shares_trade(db, account_equity=100, entry_price=200.0, stop_price=100.0)
    assert rc.ok


def test_swing_budget_yields_to_core_allocation(monkeypatch):
    monkeypatch.setattr(settings, "max_total_notional_pct", 60.0)
    monkeypatch.setattr(settings, "core_allocation_pct", 60.0)
    monkeypatch.setattr(settings, "core_enabled", True)
    assert engine.swing_budget_pct() == 40.0
    monkeypatch.setattr(settings, "core_enabled", False)
    assert engine.swing_budget_pct() == 60.0
    monkeypatch.setattr(settings, "core_enabled", True)
    monkeypatch.setattr(settings, "max_total_notional_pct", 30.0)
    assert engine.swing_budget_pct() == 30.0  # the tighter of the two


def test_total_exposure_check_uses_swing_budget(db, monkeypatch):
    monkeypatch.setattr(settings, "fractional_shares", True)
    monkeypatch.setattr(settings, "min_swing_notional_usd", 25.0)
    monkeypatch.setattr(settings, "max_total_notional_pct", 60.0)
    monkeypatch.setattr(settings, "core_allocation_pct", 60.0)
    monkeypatch.setattr(settings, "core_enabled", True)
    # three $150 swing positions = $450 deployed on $1,000 equity
    for i, t in enumerate(("AAA", "BBB", "CCC")):
        _open_position(db, t, qty=1.5, avg_price=100.0, sector=f"S{i}")
    # a 4th $100 position takes the book to $550: fine against 60%,
    # refused against the 40% the core book leaves
    rc = engine.check_shares_trade(
        db, account_equity=1_000, entry_price=100.0, stop_price=90.0, sector="S9"
    )
    assert not rc.ok
    assert not rc.checks["total_exposure"]["ok"]
    monkeypatch.setattr(settings, "core_enabled", False)
    rc = engine.check_shares_trade(
        db, account_equity=1_000, entry_price=100.0, stop_price=90.0, sector="S9"
    )
    assert rc.ok
