"""2026-11-20: an entry is bought on strength, inside its zone.

The week of 2026-11-13 the gate asked for a reclaim on yesterday's bar,
the reward-to-risk rule pushed the investor to put the entry zone below
that close, and the executor filled at any price at or under the zone
high. All three entries (SNPS, WFC, INTU) were bought below the prior
close - 2.6%, 0.8% and 6.5% - on days SPY was falling; INTU's second
verdict put the whole zone under the swing low its own thesis called the
invalidator, and the fill was a fifth of an ATR above the stop.

Now the executor fills only while the price is inside the zone and not
below the prior close, and not while the index is falling hard; the
verdict checks reject a zone that cannot fill under that rule, measure
the stop from the lowest fill the zone allows, and cap a pullback's first
target at the retest of its 20-day high. Half size halves the notional."""

import pytest

from midas.config import settings
from midas.execution import executor, paper_broker
from midas.risk import engine, verdict_checks
from tests.test_entry_rules import _thesis, _verdict


@pytest.fixture(autouse=True)
def _strength(monkeypatch):
    monkeypatch.setattr(settings, "entry_require_strength", True)
    monkeypatch.setattr(settings, "entry_max_index_drop_pct", 0.5)
    monkeypatch.setattr(settings, "min_stop_atr", 1.0)
    monkeypatch.setattr(settings, "min_reward_to_risk", 1.5)


TECH = {
    "close": 326.0,
    "atr14": 8.0,
    "setup": "pullback-in-uptrend",
    "high_20d": {"price": 345.0, "date": "2026-11-05"},
    "swing_low": {"price": 316.0, "date": "2026-11-10"},
}


# ---------- the verdict: a zone that can fill ----------


def test_a_zone_under_the_last_close_can_never_fill():
    v = _verdict(entry_zone_low=318.0, entry_zone_high=323.0, stop_price=310.0, targets=[345.0])
    (problem,) = verdict_checks.problems(v, 322.0, atr=8.0, technical=TECH)
    assert problem == (
        "entry zone 318-323 cannot fill: an order fills only between the prior close 326 and "
        "the zone high, while the reclaim is still standing, and that window is 0.00 ATR wide "
        "(minimum 0.1) - set the zone from the last close up, or rule no_trade"
    )


def test_a_single_price_zone_cannot_fill():
    v = _verdict(entry_zone_low=327.0, entry_zone_high=327.0, stop_price=317.0, targets=[345.0])
    (problem,) = verdict_checks.problems(v, 327.0, atr=8.0, technical=TECH)
    assert "cannot fill" in problem and "0.00 ATR wide" in problem


def test_a_zone_from_the_close_up_is_sane():
    v = _verdict(entry_zone_low=325.0, entry_zone_high=328.0, stop_price=317.0, targets=[345.0])
    # lowest fill 326 -> 9 above the stop (1.1 ATR); from 328: 17 reward on 11 risk
    assert verdict_checks.problems(v, 327.0, atr=8.0, technical=TECH) == []


def test_the_stop_is_measured_from_the_lowest_fill_the_zone_allows():
    v = _verdict(entry_zone_low=320.0, entry_zone_high=328.0, stop_price=319.0, targets=[345.0])
    (problem,) = verdict_checks.problems(v, 327.0, atr=8.0, technical=TECH)
    assert problem.startswith("stop 319 is 0.88 ATR from the lowest fill 326 (14-day ATR 8;")
    # without a technical block the lowest fill is the zone low itself
    (problem,) = verdict_checks.problems(v, 327.0, atr=8.0)
    assert problem.startswith("stop 319 is 0.12 ATR from the lowest fill 320 ")


def test_a_pullbacks_first_target_is_the_retest_of_its_high():
    v = _verdict(entry_zone_low=325.0, entry_zone_high=328.0, stop_price=317.0, targets=[360.0])
    (problem,) = verdict_checks.problems(v, 327.0, atr=8.0, technical=TECH)
    assert problem == (
        "first target 360 is 1.9 ATR beyond the 20-day high 345: under a 20-day high the "
        "first target is the retest of that high (within 1 ATR of it); a further level is a "
        "second target - if the retest does not pay 1.5 to 1, rule no_trade"
    )
    # a gap-and-hold under its 20-day high is held to the same retest (2026-11-23) ...
    gap = {**TECH, "setup": "gap-and-hold"}
    assert verdict_checks.problems(v, 327.0, atr=8.0, technical=gap) == [problem]
    # ... and one at its high has no overhead level to cap the target
    at_high = {**gap, "high_20d": {"price": 326.0, "date": "2026-11-19"}}
    assert verdict_checks.problems(v, 327.0, atr=8.0, technical=at_high) == []


def test_a_thin_trade_is_no_longer_told_to_raise_its_target():
    v = _verdict(entry_zone_low=325.0, entry_zone_high=328.0, stop_price=317.0, targets=[340.0])
    (problem,) = verdict_checks.problems(v, 327.0, atr=8.0, technical=TECH)
    assert problem.startswith("reward-to-risk is 1.09 to 1")
    assert "raise the target" not in problem and problem.endswith("or rule no_trade")


def test_the_strength_checks_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "entry_require_strength", False)
    v = _verdict(entry_zone_low=318.0, entry_zone_high=323.0, stop_price=305.0, targets=[355.0])
    assert verdict_checks.problems(v, 322.0, atr=8.0, technical=TECH) == []


# ---------- the executor: fill on strength, inside the zone ----------


def _quotes(monkeypatch, price, prev_close, spy=(500.0, 500.0)):
    import midas.tools.market_data as md

    class _Q:
        def __init__(self, data):
            self.data = data

    def fake(ticker):
        if ticker.upper() == "SPY":
            return _Q({"price": spy[0], "prev_close": spy[1], "sector": ""})
        return _Q({"price": price, "prev_close": prev_close, "sector": "Financial"})

    monkeypatch.setattr(md, "get_quote", fake)
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: price)


@pytest.fixture()
def account(db, monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 2)
    monkeypatch.setattr(settings, "fractional_shares", True)
    monkeypatch.setattr(settings, "min_swing_notional_usd", 25.0)
    monkeypatch.setattr(settings, "regime_filter_enabled", False)
    monkeypatch.setattr(settings, "min_reward_to_risk", 0.0)
    paper_broker.set_account(db, cash=1000.0, equity=1000.0)
    return db


def test_a_price_under_the_prior_close_waits(account, monkeypatch):
    th = _thesis(account, "AXP", lo=100.0, hi=103.0, stop=93.0, earnings_in_days=40)
    _quotes(monkeypatch, 101.0, 102.0)
    with pytest.raises(executor.AwaitingEntryZone) as exc:
        executor.execute_thesis(account, th, approved_by="test")
    assert str(exc.value) == (
        "awaiting strength: AXP 101 is below the prior close 102 - the reclaim is not "
        "holding today; re-checked each pass"
    )
    assert th.status.value == "approved"  # waiting, not rejected


def test_a_price_under_the_zone_waits(account, monkeypatch):
    th = _thesis(account, "AXP", lo=100.0, hi=103.0, stop=93.0, earnings_in_days=40)
    _quotes(monkeypatch, 99.5, 99.0)
    with pytest.raises(executor.AwaitingEntryZone) as exc:
        executor.execute_thesis(account, th, approved_by="test")
    assert str(exc.value) == (
        "awaiting entry zone: AXP 99.5 is below the zone 100-103 - the stop distance was "
        "checked from the zone, and a fill under it is closer to the stop than the "
        "thesis allows; re-checked each pass"
    )


def test_a_falling_index_waits(account, monkeypatch):
    th = _thesis(account, "AXP", lo=100.0, hi=103.0, stop=93.0, earnings_in_days=40)
    _quotes(monkeypatch, 102.0, 101.0, spy=(495.0, 500.0))
    with pytest.raises(executor.AwaitingEntryZone) as exc:
        executor.execute_thesis(account, th, approved_by="test")
    assert str(exc.value) == (
        "awaiting the tape: SPY is down 1.0% today (limit 0.5%) - no new long while the "
        "index is falling; re-checked each pass"
    )
    monkeypatch.setattr(settings, "entry_max_index_drop_pct", 0.0)  # off
    assert executor.execute_thesis(account, th, approved_by="test").status.value == "filled"


def test_strength_inside_the_zone_fills(account, monkeypatch):
    th = _thesis(account, "AXP", lo=100.0, hi=103.0, stop=93.0, earnings_in_days=40)
    _quotes(monkeypatch, 102.0, 101.0, spy=(499.0, 500.0))  # index -0.2%: inside the limit
    order = executor.execute_thesis(account, th, approved_by="test")
    assert order.status.value == "filled" and order.qty == pytest.approx(1.0)


def test_an_unknown_prior_close_or_index_does_not_block(account, monkeypatch):
    import midas.tools.market_data as md

    class _Q:
        data = {"price": 102.0, "sector": "Financial"}  # no prev_close on record

    def fake(ticker):
        if ticker.upper() == "SPY":
            raise RuntimeError("feed down")
        return _Q()

    monkeypatch.setattr(md, "get_quote", fake)
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 102.0)
    th = _thesis(account, "AXP", lo=100.0, hi=103.0, stop=93.0, earnings_in_days=40)
    assert executor.execute_thesis(account, th, approved_by="test").status.value == "filled"


def test_the_executor_rule_can_be_switched_off(account, monkeypatch):
    monkeypatch.setattr(settings, "entry_require_strength", False)
    th = _thesis(account, "AXP", lo=100.0, hi=103.0, stop=93.0, earnings_in_days=40)
    _quotes(monkeypatch, 99.5, 102.0, spy=(480.0, 500.0))
    assert executor.execute_thesis(account, th, approved_by="test").status.value == "filled"


# ---------- half size is half the position ----------


def test_half_size_halves_the_notional_cap_too(db, monkeypatch):
    # INTU 2026-11-17: "1.0% x 0.5 -> 0.604 shares", then "capped to 0.393"
    # by the notional cap - the same size as a full-risk entry
    monkeypatch.setattr(settings, "max_notional_pct_per_trade", 15.0)
    monkeypatch.setattr(settings, "min_reward_to_risk", 0.0)
    kw = dict(account_equity=1000.0, entry_price=100.0, stop_price=99.0)
    full = engine.check_shares_trade(db, **kw)
    half = engine.check_shares_trade(db, **kw, risk_scale=0.5)
    assert full.max_qty == 1.5 and half.max_qty == 0.75
    assert half.checks["notional_cap"]["limit"] == "<= 15.0% x 0.5 of equity notional"
