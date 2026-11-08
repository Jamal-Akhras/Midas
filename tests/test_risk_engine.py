"""Risk engine: deterministic sizing and caps. These tests are the spec."""

from datetime import timedelta

from midas.config import settings
from midas.models import Instrument, Position, PositionStatus, TradeOutcome, TradeThesis, utcnow
from midas.models.domain import Direction, PipelineRun, ResearchArtifact, RunKind, ThesisStatus
from midas.risk import engine


def _thesis(db, ticker="ACME"):
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
        entry_zone_low=99,
        entry_zone_high=101,
        stop_price=95,
        status=ThesisStatus.executed,
    )
    db.add(thesis)
    db.flush()
    return thesis


def _open_position(
    db, ticker="ACME", sector="Tech", instrument=Instrument.shares, qty=10.0, avg_price=100.0
):
    thesis = _thesis(db, ticker)
    p = Position(
        thesis_id=thesis.id,
        ticker=ticker,
        symbol=ticker,
        instrument=instrument,
        sector=sector,
        qty=qty,
        avg_price=avg_price,
        opened_at=utcnow(),
    )
    db.add(p)
    db.flush()
    return p


def test_shares_sizing_respects_per_trade_risk(db, monkeypatch):
    monkeypatch.setattr(settings, "max_notional_pct_per_trade", 100.0)
    rc = engine.check_shares_trade(db, account_equity=100_000, entry_price=100.0, stop_price=95.0)
    assert rc.ok
    # 1% of 100k = $1000 risk budget; $5 risk/share -> 200 shares
    assert rc.max_qty == 200


def test_shares_rejects_stop_above_entry(db):
    rc = engine.check_shares_trade(db, account_equity=100_000, entry_price=95.0, stop_price=100.0)
    assert not rc.ok
    assert not rc.checks["stop_below_entry"]["ok"]


def test_option_sizing_uses_tightest_premium_cap(db):
    rc = engine.check_option_trade(db, account_equity=100_000, premium_per_contract=4.0, dte=45)
    assert rc.ok
    # min(1%, 2%) of 100k = $1000; $400/contract -> 2 contracts
    assert rc.max_qty == 2


def test_option_rejects_short_dte(db):
    rc = engine.check_option_trade(db, account_equity=100_000, premium_per_contract=1.0, dte=7)
    assert not rc.ok
    assert not rc.checks["min_dte"]["ok"]


def test_total_premium_cap_counts_open_options(db):
    # an open option position eating most of the 10% total premium room
    _open_position(
        db, ticker="BOLT", instrument=Instrument.option, qty=24, avg_price=4.0
    )  # 24*400 = $9,600 of $10,000 cap
    rc = engine.check_option_trade(db, account_equity=100_000, premium_per_contract=4.0, dte=45)
    # $400 of room remains -> exactly 1 contract, not the 2 the per-trade
    # cap alone would allow
    assert rc.ok
    assert rc.max_qty == 1


def test_total_premium_cap_boundary(db):
    _open_position(
        db, ticker="BOLT", instrument=Instrument.option, qty=24, avg_price=4.1
    )  # $9,840 open premium
    rc = engine.check_option_trade(db, account_equity=100_000, premium_per_contract=4.0, dte=45)
    assert not rc.ok  # $160 room cannot fit a $400 contract


def test_max_open_positions(db, monkeypatch):
    monkeypatch.setattr(settings, "max_open_positions", 2)
    _open_position(db, "ACME")
    _open_position(db, "BOLT")
    rc = engine.check_shares_trade(db, account_equity=100_000, entry_price=100, stop_price=95)
    assert not rc.ok
    assert not rc.checks["max_open_positions"]["ok"]


def test_sector_concentration(db, monkeypatch):
    monkeypatch.setattr(settings, "max_positions_per_sector", 1)
    _open_position(db, "ACME", sector="Tech")
    rc = engine.check_shares_trade(
        db, account_equity=100_000, entry_price=100, stop_price=95, sector="Tech"
    )
    assert not rc.ok
    assert not rc.checks["sector_concentration"]["ok"]


def test_halt_blocks_everything(db):
    engine.set_halt(db, "daily loss limit")
    rc = engine.check_shares_trade(db, account_equity=100_000, entry_price=100, stop_price=95)
    assert not rc.ok
    assert not rc.checks["pipeline_not_halted"]["ok"]
    engine.clear_halt(db)
    rc = engine.check_shares_trade(db, account_equity=100_000, entry_price=100, stop_price=95)
    assert rc.ok


def test_daily_loss_limit_sets_halt(db):
    thesis = _thesis(db)
    p = _open_position(db, "BOLT")
    p.status = PositionStatus.closed
    db.add(
        TradeOutcome(
            position_id=p.id,
            thesis_id=thesis.id,
            pnl_usd=-3500.0,
            pnl_pct=-35,
            holding_days=3,
            exit_kind="stop",
        )
    )
    db.flush()
    reason = engine.enforce_loss_halt(db, account_equity=100_000)
    assert reason and "daily loss limit" in reason
    assert engine.get_halt(db)["active"]


def test_weekly_loss_limit(db, monkeypatch):
    thesis = _thesis(db)
    p = _open_position(db, "BOLT")
    p.status = PositionStatus.closed
    outcome = TradeOutcome(
        position_id=p.id,
        thesis_id=thesis.id,
        pnl_usd=-6500.0,
        pnl_pct=-65,
        holding_days=3,
        exit_kind="stop",
    )
    db.add(outcome)
    db.flush()
    # push it out of today's window but inside the week
    outcome.created_at = utcnow() - timedelta(days=1)
    db.flush()
    monkeypatch.setattr(settings, "daily_loss_limit_pct", 100.0)
    reason = engine.loss_limits_breached(db, account_equity=100_000)
    if utcnow().weekday() == 0:
        assert reason is None  # Monday: yesterday was last week - boundary is honest
    else:
        assert reason is not None
        assert "weekly" in reason


def test_notional_cap_limits_tight_stop_positions(db, monkeypatch):
    monkeypatch.setattr(settings, "max_notional_pct_per_trade", 15.0)
    # tight stop: risk sizing alone would allow 1000 shares ($100k notional)
    rc = engine.check_shares_trade(db, account_equity=100_000, entry_price=100.0, stop_price=99.0)
    assert rc.ok
    # 15% of 100k = $15k / $100 = 150 shares, not 1000
    assert rc.max_qty == 150
    assert rc.checks["notional_cap"]["ok"]


def test_total_exposure_budget_binds_across_positions(db, monkeypatch):
    monkeypatch.setattr(settings, "max_total_notional_pct", 60.0)
    monkeypatch.setattr(settings, "max_open_positions", 10)
    # four open 15%-notional positions = 60% deployed; budget is full
    for t in ("AAA", "BBB", "CCC", "DDD"):
        _open_position(db, ticker=t, qty=150, avg_price=100.0)  # $15k each
    rc = engine.check_shares_trade(db, account_equity=100_000, entry_price=100.0, stop_price=99.0)
    assert not rc.ok
    assert not rc.checks["total_exposure"]["ok"]


def test_total_exposure_allows_within_budget(db, monkeypatch):
    monkeypatch.setattr(settings, "max_total_notional_pct", 60.0)
    _open_position(db, ticker="AAA", qty=150, avg_price=100.0)  # 15% deployed
    rc = engine.check_shares_trade(db, account_equity=100_000, entry_price=100.0, stop_price=95.0)
    assert rc.ok
    assert rc.checks["total_exposure"]["ok"]
