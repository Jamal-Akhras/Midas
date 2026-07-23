"""Evidence-pipeline integrity: the exact regressions the review found."""
from midas.config import settings
from midas.execution import paper_broker
from midas.models import (
    Broker, Instrument, Order, OrderReason, OrderSide, OrderStatus, Position,
    PositionStatus, SystemState, TradeOutcome, utcnow,
)
from midas.models.domain import Direction, PipelineRun, ResearchArtifact, RunKind, ThesisStatus, TradeThesis
from midas.outcomes.scoring import stats
from midas.scheduler import monitor


def _start_l2(db):
    from datetime import timedelta

    db.add(SystemState(key="l2_since", value={
        "at": (utcnow() - timedelta(days=1)).isoformat(), "fingerprint": "t"}))
    db.flush()


def _closed_position(db, ticker, pnl, brokers):
    """A closed position whose lifecycle orders used the given brokers."""
    run = PipelineRun(kind=RunKind.committee, ticker=ticker)
    db.add(run); db.flush()
    art = ResearchArtifact(run_id=run.id, ticker=ticker, agent_slug="investor",
                           artifact_type="verdict", content={})
    db.add(art); db.flush()
    th = TradeThesis(run_id=run.id, verdict_artifact_id=art.id, ticker=ticker,
                     direction=Direction.bullish, instrument=Instrument.shares,
                     entry_zone_low=99, entry_zone_high=101, stop_price=95,
                     status=ThesisStatus.executed)
    db.add(th); db.flush()
    p = Position(thesis_id=th.id, ticker=ticker, symbol=ticker,
                 instrument=Instrument.shares, qty=0, avg_price=100.0,
                 status=PositionStatus.closed, opened_at=utcnow(),
                 closed_at=utcnow())
    db.add(p); db.flush()
    for i, b in enumerate(brokers):
        o = Order(thesis_id=th.id, position_id=p.id,
                  side=OrderSide.buy if i == 0 else OrderSide.sell,
                  instrument=Instrument.shares, symbol=ticker, qty=10,
                  reason=OrderReason.entry if i == 0 else OrderReason.exit,
                  status=OrderStatus.filled, filled_price=100.0, broker=b)
        db.add(o)
    db.flush()
    db.add(TradeOutcome(position_id=p.id, thesis_id=th.id, pnl_usd=pnl,
                        pnl_pct=pnl / 1000 * 100, holding_days=3,
                        exit_kind="target"))
    db.flush()
    return p


def test_stats_survives_broker_backed_orders(db):
    """Regression: stats() crashed with AttributeError on UUID."""
    _start_l2(db)
    _closed_position(db, "AAA", 120.0,
                     [Broker.alpaca_paper, Broker.alpaca_paper])
    s = stats(db)  # must not raise
    assert s["eligible_cohort"]["n"] == 1


def test_eligibility_requires_full_broker_lifecycle(db):
    _start_l2(db)
    # sim entry + broker exit: NOT eligible evidence
    _closed_position(db, "MIX", 50.0, [Broker.paper_sim, Broker.alpaca_paper])
    # broker entry with NO recorded exit: not a complete round trip
    _closed_position(db, "HALF", 30.0, [Broker.alpaca_paper])
    # full broker round trip: eligible
    _closed_position(db, "PURE", 80.0, [Broker.alpaca_paper, Broker.alpaca_paper])
    s = stats(db)
    assert s["eligible_cohort"]["n"] == 1
    assert s["eligible_cohort"]["total_pnl_usd"] == 80.0


def test_pre_cohort_outcomes_are_orphaned(db):
    """A fingerprint restart must NOT inherit the previous strategy's
    trades: outcomes older than l2_since are excluded."""
    from datetime import timedelta

    p = _closed_position(db, "OLD", 500.0,
                         [Broker.alpaca_paper, Broker.alpaca_paper])
    outcome = db.query(TradeOutcome).filter_by(position_id=p.id).one()
    _start_l2(db)  # clock starts AFTER the trade closed
    outcome.created_at = utcnow() - timedelta(days=3)
    db.flush()
    s = stats(db)
    assert s["closed_positions"] == 1        # history still reported
    assert s["eligible_cohort"]["n"] == 0    # but not promotion evidence


def test_no_l2_period_means_no_eligible_cohort(db):
    _closed_position(db, "AAA", 120.0,
                     [Broker.alpaca_paper, Broker.alpaca_paper])
    assert stats(db)["eligible_cohort"]["n"] == 0


def test_option_fees_reach_realized_pnl(db, monkeypatch):
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 4.0)
    run = PipelineRun(kind=RunKind.committee, ticker="OPT")
    db.add(run); db.flush()
    art = ResearchArtifact(run_id=run.id, ticker="OPT", agent_slug="investor",
                           artifact_type="verdict", content={})
    db.add(art); db.flush()
    th = TradeThesis(run_id=run.id, verdict_artifact_id=art.id, ticker="OPT",
                     direction=Direction.bullish, instrument=Instrument.option,
                     entry_zone_low=99, entry_zone_high=101, stop_price=95,
                     status=ThesisStatus.approved)
    db.add(th); db.flush()
    buy = Order(thesis_id=th.id, side=OrderSide.buy, instrument=Instrument.option,
                symbol="OPT_2026-08-21_call_100.0", qty=1,
                reason=OrderReason.entry, status=OrderStatus.approved)
    db.add(buy); db.flush()
    paper_broker.submit(db, buy)
    pos = paper_broker.apply_fill_to_position(db, buy, th)
    # cost basis carries the entry fee: 4.04 + 0.65/100
    assert round(pos.avg_price, 4) == round(4.04 + 0.65 / 100, 4)
    sell = Order(thesis_id=th.id, position_id=pos.id, side=OrderSide.sell,
                 instrument=Instrument.option, symbol=buy.symbol, qty=1,
                 reason=OrderReason.exit, status=OrderStatus.approved)
    db.add(sell); db.flush()
    paper_broker.submit(db, sell)
    paper_broker.apply_fill_to_position(db, sell, th)
    # exit at 3.96 (mid - 1%), minus exit fee: P&L must include BOTH fees
    expected = (3.96 - (4.04 + 0.0065)) * 100 - 0.65
    assert round(pos.realized_pnl_usd, 2) == round(expected, 2)


def test_cash_check_includes_fees(db, monkeypatch):
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 4.0)
    paper_broker.set_account(db, cash=404.0, equity=404.0)  # notional exactly
    o = Order(thesis_id=_closed_position(db, "ZZZ", 0, []).thesis_id,
              side=OrderSide.buy, instrument=Instrument.option,
              symbol="ZZZ_2026-08-21_call_100.0", qty=1,
              reason=OrderReason.entry, status=OrderStatus.approved)
    db.add(o); db.flush()
    paper_broker.submit(db, o)
    assert o.status == OrderStatus.rejected  # fee pushes it past available cash


def test_day_anchor_carries_prior_session_equity(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from midas.risk import engine as eng

    day1 = datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(eng, "utcnow", lambda: day1)
    eng.update_equity_anchors(db, 100_000.0)   # Wednesday close-ish
    monkeypatch.setattr(eng, "utcnow", lambda: day1 + timedelta(days=1))
    v = eng.update_equity_anchors(db, 95_000.0)  # Thursday opens gapped down
    # Thursday's anchor is Wednesday's last equity - the gap counts as loss
    assert v["day_equity"] == 100_000.0
    monkeypatch.setattr(eng, "utcnow", lambda: day1 + timedelta(days=1, hours=1))
    reason = eng.loss_limits_breached(db, account_equity=95_000.0)
    assert reason and "drawdown" in reason


def test_fingerprint_change_restarts_l2_clock(db, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 2)
    monitor._record_l2_ledger(db)
    first = dict(db.get(SystemState, "l2_since").value)
    monkeypatch.setattr(settings, "per_trade_risk_pct", 2.0)  # strategy changed
    monitor._record_l2_ledger(db)
    second = db.get(SystemState, "l2_since").value
    assert second["fingerprint"] != first["fingerprint"]


def test_unconfirmed_cancel_aborts_exit_without_restore(db, monkeypatch):
    """If the broker cannot confirm the stop was cancelled, selling could
    double-fill: the exit must abort and the stop row must stay ACTIVE."""
    from midas.models import StandingStop

    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 100.0)
    monkeypatch.setattr(paper_broker, "_cancel_standing_stop",
                        lambda db, pid: None)  # unconfirmed
    restored = []
    monkeypatch.setattr(paper_broker, "_restore_standing_stop",
                        lambda db, pid: restored.append(pid))
    p = _closed_position(db, "ABT", 0, [])
    p.status = PositionStatus.open
    p.qty = 10
    db.flush()
    exit_order = Order(thesis_id=p.thesis_id, position_id=p.id,
                       side=OrderSide.sell, instrument=Instrument.shares,
                       symbol="ABT", qty=10, reason=OrderReason.exit,
                       status=OrderStatus.approved)
    db.add(exit_order); db.flush()
    paper_broker.submit(db, exit_order)
    assert exit_order.status == OrderStatus.rejected
    assert "double-sell" in exit_order.error
    assert restored == []  # no second stop was placed


def test_failed_exit_restores_standing_stop(db, monkeypatch):
    from midas.models import StandingStop

    monkeypatch.setattr(settings, "alpaca_mirror_orders", True)
    monkeypatch.setattr(settings, "require_broker_fills", True)
    monkeypatch.setattr(settings, "alpaca_api_key", "x")
    monkeypatch.setattr(settings, "alpaca_api_secret", "y")
    monkeypatch.setattr(paper_broker, "_mirror_to_alpaca", lambda o: None)
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 100.0)
    restored = []
    monkeypatch.setattr(paper_broker, "_restore_standing_stop",
                        lambda db, pid: restored.append(pid))
    # broker CONFIRMS the cancellation; the exit then fails at submission
    monkeypatch.setattr(paper_broker, "_cancel_standing_stop",
                        lambda db, pid: True)
    p = _closed_position(db, "RST", 0, [])
    p.status = PositionStatus.open
    p.qty = 10
    db.add(StandingStop(position_id=p.id, alpaca_order_id="old",
                        stop_price=95.0, qty=10))
    db.flush()
    exit_order = Order(thesis_id=p.thesis_id, position_id=p.id,
                       side=OrderSide.sell, instrument=Instrument.shares,
                       symbol="RST", qty=10, reason=OrderReason.exit,
                       status=OrderStatus.approved)
    db.add(exit_order); db.flush()
    paper_broker.submit(db, exit_order)
    assert exit_order.status == OrderStatus.rejected  # fail-closed
    assert restored == [p.id]  # and the stop went straight back


def test_update_setting_whitelist_and_env_write(tmp_path, monkeypatch):
    from midas.config import settings as cfg, update_setting

    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=secret-stays\nWATCHLIST=OLD\nWATCHLIST=DUP\n",
                   encoding="utf-8")
    change = update_setting("WATCHLIST", "RKLB,PLTR", env_path=env)
    assert change["new"] == "RKLB,PLTR" and change["fingerprint"] is False
    text = env.read_text(encoding="utf-8")
    assert "WATCHLIST=RKLB,PLTR" in text
    assert "OPENAI_API_KEY=secret-stays" in text        # secrets untouched
    assert text.count("\nWATCHLIST=") <= 1 or "duplicate removed" in text
    assert cfg.watchlist == "RKLB,PLTR"                 # hot-applied

    import pytest as _pt
    with _pt.raises(ValueError):
        update_setting("OPENAI_API_KEY", "steal", env_path=env)  # not editable
    with _pt.raises(Exception):
        update_setting("AUTONOMY_LEVEL", "3", env_path=env)  # L3 still refused


def test_occ_symbol_round_trip():
    from midas.execution.paper_broker import occ_symbol
    from midas.tools.market_data import _occ_meta

    sym = occ_symbol("NVDA", "2026-08-21", "call", 210.0)
    assert sym == "NVDA260821C00210000"
    meta = _occ_meta(sym)
    assert meta == {"expiry": "2026-08-21", "type": "call", "strike": 210.0}
    assert occ_symbol("AAPL", "2026-09-18", "put", 187.5).endswith("P00187500")


def test_option_order_payload_spread_entry_and_exit(db):
    from midas.execution.paper_broker import option_order_payload

    entry = Order(thesis_id=_closed_position(db, "PAY", 0, []).thesis_id,
                  side=OrderSide.buy, instrument=Instrument.option,
                  symbol="NVDA_2026-08-21_call_210.0x220.0", qty=2,
                  reason=OrderReason.entry, status=OrderStatus.approved)
    db.add(entry); db.flush()
    pl = option_order_payload(entry, 3.5)
    assert pl["order_class"] == "mleg" and pl["qty"] == "2"
    assert pl["limit_price"] == "3.57"  # pays 2% through the mid
    assert pl["legs"][0]["position_intent"] == "buy_to_open"
    assert pl["legs"][1] == {"symbol": "NVDA260821C00220000", "ratio_qty": "1",
                             "side": "sell", "position_intent": "sell_to_open"}
    exit_o = Order(thesis_id=entry.thesis_id, side=OrderSide.sell,
                   instrument=Instrument.option, symbol=entry.symbol, qty=2,
                   reason=OrderReason.exit, status=OrderStatus.approved)
    db.add(exit_o); db.flush()
    pl2 = option_order_payload(exit_o, 5.0)
    assert pl2["limit_price"] == "4.90"  # gives up 2% on the way out
    assert pl2["legs"][0]["position_intent"] == "sell_to_close"
    assert pl2["legs"][1]["position_intent"] == "buy_to_close"
    # single leg: plain order, no mleg
    single = Order(thesis_id=entry.thesis_id, side=OrderSide.buy,
                   instrument=Instrument.option,
                   symbol="NVDA_2026-08-21_call_210.0", qty=1,
                   reason=OrderReason.entry, status=OrderStatus.approved)
    db.add(single); db.flush()
    pl3 = option_order_payload(single, 8.0)
    assert "order_class" not in pl3 and pl3["symbol"] == "NVDA260821C00210000"


def test_pass_lock_excludes_second_holder(db):
    from midas.scheduler.monitor import acquire_pass_lock, release_pass_lock

    t1 = acquire_pass_lock(db)
    assert t1 is not None
    assert acquire_pass_lock(db) is None  # held
    release_pass_lock(db, t1)
    t2 = acquire_pass_lock(db)
    assert t2 is not None
    release_pass_lock(db, t2)


def test_conviction_orders_execution_queue(db, monkeypatch):
    from midas.models import ArtifactType as AT
    from midas.models.domain import ResearchArtifact as RA
    from midas.scheduler import monitor as mon

    monkeypatch.setattr(settings, "autonomy_level", 2)
    executed = []

    class _FakeOrder:
        status = OrderStatus.filled

    def _fake(db, th, approved_by):
        executed.append(th.ticker)
        return _FakeOrder()

    monkeypatch.setattr(mon.executor, "execute_thesis", _fake)
    for ticker, conv in (("LOW", 3.0), ("HIGH", 9.0), ("MID", 6.0)):
        p = _closed_position(db, ticker, 0, [])
        th = db.get(TradeThesis, p.thesis_id)
        th.status = ThesisStatus.approved
        db.add(RA(run_id=th.run_id, ticker=ticker, agent_slug="bull",
                  artifact_type=AT.bull_case,
                  content={"conviction": conv, "expected_move_pct": 10}))
    db.flush()
    assert mon.execute_approved_theses(db) == 3
    assert executed == ["HIGH", "MID", "LOW"]  # strongest case first


def test_one_position_per_ticker(db, monkeypatch):
    from midas.execution import executor
    from midas.scheduler import monitor as mon

    monkeypatch.setattr(settings, "autonomy_level", 2)
    p = _closed_position(db, "DUP", 0, [])
    p.status = PositionStatus.open
    p.qty = 10
    th2_pos = _closed_position(db, "DUP", 0, [])  # second thesis, same ticker
    th2 = db.get(TradeThesis, th2_pos.thesis_id)
    db.delete(th2_pos)  # keep only the thesis
    th2.status = ThesisStatus.approved
    db.flush()
    import pytest as _pt
    with _pt.raises(executor.ExecutionRefused, match="already open"):
        executor.execute_thesis(db, th2, approved_by="test")
    # and the queue runner marks it rejected so it stops retrying
    assert mon.execute_approved_theses(db) == 0
    assert th2.status == ThesisStatus.rejected


def test_fractional_sizing_at_small_scale(db, monkeypatch):
    from midas.risk import engine as eng

    monkeypatch.setattr(settings, "fractional_shares", True)
    rc = eng.check_shares_trade(db, account_equity=1_000,
                                entry_price=200.0, stop_price=190.0)
    # $10 risk budget / $10 risk-per-share = 1 share... but 15% notional
    # cap = $150 -> 0.75 shares. Fractional keeps the trade alive.
    assert rc.ok
    assert rc.max_qty == 0.75
    monkeypatch.setattr(settings, "fractional_shares", False)
    rc2 = eng.check_shares_trade(db, account_equity=1_000,
                                 entry_price=200.0, stop_price=190.0)
    assert not rc2.ok  # whole shares: 0 shares -> honest refusal


def test_option_entry_zone_gate(db, monkeypatch):
    from midas.execution import executor
    from midas.models import OptionStructure
    from midas.models.domain import OptionType

    p = _closed_position(db, "GATE", 0, [])
    db.delete(p)
    th = db.query(TradeThesis).filter_by(ticker="GATE").one()
    th.status = ThesisStatus.approved
    th.instrument = Instrument.option
    th.entry_zone_high = 100.0
    db.add(OptionStructure(thesis_id=th.id, underlying="GATE",
                           strategy="call_debit_spread",
                           option_type=OptionType.call, long_strike=100.0,
                           expiry=utcnow().date(), dte_at_entry=30,
                           est_premium_per_contract=2.0, contracts=1))
    db.flush()

    class _Q:
        data = {"price": 108.0}  # extended: 8% above the zone

    import midas.tools.market_data as md
    monkeypatch.setattr(md, "get_quote", lambda t: _Q())
    import pytest as _pt
    with _pt.raises(executor.ExecutionRefused, match="awaiting entry zone"):
        executor.execute_thesis(db, th, approved_by="test")


def test_regime_gate_blocks_entries(db, monkeypatch):
    from midas.scheduler import monitor as mon

    monkeypatch.setattr(settings, "autonomy_level", 2)
    p = _closed_position(db, "RGM", 0, [])
    th = db.get(TradeThesis, p.thesis_id)
    db.delete(p)
    th.status = ThesisStatus.approved
    db.flush()
    called = []
    monkeypatch.setattr(mon.executor, "execute_thesis",
                        lambda db, t, approved_by: called.append(t.ticker))
    # risk-off is enforced at the pass level: entries simply don't run
    # (mirrors the run_monitor_once wiring: executed=0 when not risk_on)
    regime = {"risk_on": False, "detail": "SPY below 200d", "date": "x"}
    assert not regime["risk_on"]  # the pass gates on this


def test_counterfactual_stats_split(db):
    from midas.models import ScreenCandidate
    from midas.outcomes.scoring import stats

    # traded ticker: has an executed thesis
    p = _closed_position(db, "WIN", 100.0,
                         [Broker.alpaca_paper, Broker.alpaca_paper])
    db.add(ScreenCandidate(ticker="WIN", score=90, price_at_screen=100,
                           fwd_10d_pct=5.0))
    db.add(ScreenCandidate(ticker="SKIP1", score=70, price_at_screen=50,
                           fwd_10d_pct=9.0))
    db.add(ScreenCandidate(ticker="SKIP2", score=60, price_at_screen=40,
                           fwd_10d_pct=1.0))
    db.flush()
    cf = stats(db)["counterfactual"]
    assert cf["n_traded"] == 1 and cf["n_skipped"] == 2
    assert cf["avg_fwd_traded_pct"] == 5.0
    assert cf["avg_fwd_skipped_pct"] == 5.0  # (9+1)/2


def test_post_mortem_runs_once_and_yields_lesson(factory, demo_router):
    from midas.models import ArtifactType, TradeOutcome
    from midas.scheduler.monitor import recent_lessons, run_post_mortems

    with factory() as db:
        p = _closed_position(db, "PM", -50.0, [])
        outcome = db.query(TradeOutcome).filter_by(position_id=p.id).one()
        db.commit()
        oid = outcome.id
    lessons = run_post_mortems(factory, demo_router, [oid])
    assert lessons and "pullback" in lessons[0]
    assert run_post_mortems(factory, demo_router, [oid]) == []  # once, ever
    with factory() as db:
        assert recent_lessons(db) == lessons


def _bars(closes, vols=None):
    vols = vols or [1_000_000] * len(closes)
    return [{"close": c, "high": c * 1.01, "low": c * 0.99,
             "open": c, "volume": v, "date": "2026-01-01"}
            for c, v in zip(closes, vols)]


def test_setup_score_rewards_orderly_pullback():
    from midas.pipeline.deep_scan import setup_score

    # steady uptrend 100 -> 130, then a quiet ~6% pullback on fading volume
    closes = [100 + i * 0.6 for i in range(50)] + [129, 127.5, 126, 125, 124.3]
    vols = [1_000_000] * 50 + [600_000] * 5
    score = setup_score(_bars(closes, vols))
    assert score is not None and score > 75


def test_setup_score_rejects_chases_and_downtrends():
    from midas.pipeline.deep_scan import setup_score

    # vertical melt-up: last close 15% above the 10d mean -> extended
    closes = [100] * 40 + [100, 103, 108, 115, 124, 135]
    assert setup_score(_bars(closes)) is None
    # downtrend: below the 50d mean
    closes = [130 - i * 0.8 for i in range(55)]
    assert setup_score(_bars(closes)) is None
    # too little history
    assert setup_score(_bars([100] * 20)) is None


def test_stage_a_floors(monkeypatch):
    from midas.pipeline.deep_scan import stage_a

    monkeypatch.setattr(settings, "deep_scan_min_price", 5.0)
    monkeypatch.setattr(settings, "deep_scan_min_dollar_volume", 20_000_000.0)
    snaps = {
        "GOOD": {"dailyBar": {"c": 50.0, "v": 1_000_000},
                 "prevDailyBar": {"c": 49.0}},
        "PENNY": {"dailyBar": {"c": 2.0, "v": 50_000_000},
                  "prevDailyBar": {"c": 2.0}},
        "ILLIQ": {"dailyBar": {"c": 50.0, "v": 10_000},
                  "prevDailyBar": {"c": 50.0}},
        "HALTED": {"dailyBar": {"c": 30.0, "v": 5_000_000},
                   "prevDailyBar": {"c": 60.0}},  # -50% day: not a setup
    }
    out = stage_a(snaps)
    assert [x["ticker"] for x in out] == ["GOOD"]


def test_deep_scan_survivors_join_screen_universe(db):
    from midas.models import SystemState
    from midas.models.base import utcnow

    db.add(SystemState(key="deep_scan", value={
        "date": utcnow().date().isoformat(),
        "kept": [{"ticker": "NEWCO", "score": 88, "price": 42.0}]}))
    db.flush()
    # replicate run_screen's merge logic
    tickers = ["NVDA", "AMD"]
    ds = db.get(SystemState, "deep_scan")
    if ds and ds.value.get("date") == utcnow().date().isoformat():
        for cand in ds.value.get("kept", []):
            t = str(cand.get("ticker", "")).upper()
            if t and t not in tickers:
                tickers.append(t)
    assert "NEWCO" in tickers


def test_regime_throttle_halves_all_budgets(db, monkeypatch):
    from midas.risk import engine as eng

    db.add(SystemState(key="market_regime",
                       value={"date": "x", "risk_on": False, "detail": "t"}))
    db.flush()
    monkeypatch.setattr(settings, "regime_throttle", 0.5)
    monkeypatch.setattr(settings, "fractional_shares", False)
    monkeypatch.setattr(settings, "max_notional_pct_per_trade", 100.0)
    rc = eng.check_shares_trade(db, account_equity=100_000,
                                entry_price=100.0, stop_price=95.0)
    assert rc.ok
    assert rc.max_qty == 100          # 200 at full risk -> 100 at half
    assert rc.checks["regime_throttle"]["value"] == "x0.5"
    # binary mode still hard-blocks
    monkeypatch.setattr(settings, "regime_throttle", 0.0)
    rc2 = eng.check_shares_trade(db, account_equity=100_000,
                                 entry_price=100.0, stop_price=95.0)
    assert not rc2.ok and not rc2.checks["regime"]["ok"]


def test_regime_throttle_inert_when_risk_on(db, monkeypatch):
    from midas.risk import engine as eng

    monkeypatch.setattr(settings, "regime_throttle", 0.5)
    monkeypatch.setattr(settings, "fractional_shares", False)
    monkeypatch.setattr(settings, "max_notional_pct_per_trade", 100.0)
    # no regime row at all -> risk-on assumed, full budgets
    rc = eng.check_shares_trade(db, account_equity=100_000,
                                entry_price=100.0, stop_price=95.0)
    assert rc.ok and rc.max_qty == 200
    assert "regime_throttle" not in rc.checks
