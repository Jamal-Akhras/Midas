"""Replaying the account as it is configured, over the years the data holds.

2026-11-24: "how would the current strategy have done over the past N
years" had no answer in one place. The core book was in docs/results.md at
fixed shares; the swing clone still traded the rules of early November (a
5% stop, eight positions, the next open). midas.research.account replays
both books under one configuration: the core slices with their T-bill
sleeve, and the swing rules as they stand in code - the reclaim, the
structural stop, the room to the 20-day high, the entry on strength, the
binary regime block and the trial that ends the book.

What it cannot replay is the committee: a model asked about 2019 already
knows 2020. The swing leg is the deterministic skeleton, ranked by the
scan score."""

import numpy as np
import pandas as pd
import pytest

from midas.research import account
from midas.research.cli import main as research_main
from midas.research.data import PriceData, synthetic
from midas.research.swing_clone import midas_clone, strength_fill, structure_stop, trial_failed

CORE = ["SPY", "QQQ", "EFA", "GLD", "IEF"]


def _bundle(n_stocks: int = 30, days: int = 900, seed: int = 4, **kw) -> PriceData:
    names = [f"S{i:03d}" for i in range(n_stocks)]
    d = synthetic([*CORE, *names], days=days, seed=seed, **kw)
    return PriceData(
        open=d.open,
        high=d.high,
        low=d.low,
        close=d.close,
        volume=d.volume * 100,  # past the liquidity floor
        cash_daily=d.cash_daily,
        universes={"sp500": names},
    )


def _flat(paths: dict[str, list[float]], cash_rate: float = 0.04) -> PriceData:
    idx = pd.bdate_range("2020-01-01", periods=len(next(iter(paths.values()))))
    close = pd.DataFrame(paths, index=idx)
    return PriceData(
        open=close.shift(1).bfill(),
        high=close * 1.001,
        low=close * 0.999,
        close=close,
        volume=pd.DataFrame(1e8, index=idx, columns=close.columns),
        cash_daily=pd.Series((1 + cash_rate) ** (1 / 252) - 1, index=idx, name="CASH"),
        universes={"sp500": []},
    )


# ---------- the swing rules, one at a time ----------


def test_the_structural_stop_is_the_swing_low_but_never_inside_one_atr():
    high = np.array([10.0, 11.0, 12.0, 11.5, 11.2])
    low = np.array([9.5, 10.5, 11.4, 10.8, 10.9])
    # the high of the window is bar 2; the lowest low since then is 10.8
    assert structure_stop(high, low, close=11.0, atr=0.1, min_atr=1.0) == pytest.approx(10.8)
    # 10.8 is 0.4 ATR under the close when the ATR is 0.5: the ATR floor takes over
    assert structure_stop(high, low, close=11.0, atr=0.5, min_atr=1.0) == pytest.approx(10.5)


def test_an_entry_on_strength_fills_at_or_above_the_prior_close_inside_the_zone():
    zone = {"zone_low": 100.0, "zone_high": 102.0}
    # opens inside the window: filled at the open
    assert strength_fill(open_=101.0, high=103.0, low=100.5, prior_close=100.0, **zone) == 101.0
    # opens under the prior close and climbs back through it: filled as it reclaims
    assert strength_fill(open_=99.0, high=101.0, low=98.5, prior_close=100.0, **zone) == 100.0
    # opens above the zone and comes back into it: filled at the top of the zone
    assert strength_fill(open_=103.0, high=104.0, low=101.5, prior_close=100.0, **zone) == 102.0
    # never gets back to the prior close: the reclaim is not standing, no fill
    assert strength_fill(open_=99.0, high=99.8, low=98.0, prior_close=100.0, **zone) is None
    # gaps above the zone and never returns: chasing, no fill
    assert strength_fill(open_=103.0, high=105.0, low=102.5, prior_close=100.0, **zone) is None
    # the prior close itself is above the zone: nothing inside the zone is strength
    assert strength_fill(open_=102.5, high=104.0, low=101.0, prior_close=103.0, **zone) is None


def test_the_trial_is_read_from_its_first_trades_only():
    assert trial_failed([-1.0, 2.0, -1.0], 3) is True  # average zero: at or below
    assert trial_failed([-1.0, 2.0, -0.5], 3) is False
    assert trial_failed([-1.0, -2.0], 3) is False  # not enough trades yet
    assert trial_failed([-1.0, -2.0, -3.0, 50.0], 3) is True  # the fourth is not read
    assert trial_failed([-1.0, -2.0, -3.0], 0) is False  # no trial configured


# ---------- the swing leg as configured ----------


def test_the_live_swing_rules_hold_one_position_inside_its_cap():
    d = _bundle()
    rules = account.SwingRules(trial_trades=0)
    _, stats = midas_clone(d, d.universes["sp500"], start="2010-06-01", **rules.clone_kwargs())
    assert stats.n_trades > 5
    assert stats.max_exposure <= 0.13 * 1.25  # sized at 13%, then it drifts with the price
    assert stats.committees >= stats.n_trades  # every entry had a committee behind it
    assert len(stats.trade_pcts) == stats.n_trades


def test_a_failed_trial_ends_the_swing_book():
    d = _bundle()
    names = d.universes["sp500"]
    free = account.SwingRules(trial_trades=0).clone_kwargs()
    _, unlimited = midas_clone(d, names, start="2010-06-01", **free)
    n = next(
        k
        for k in range(3, unlimited.n_trades)
        if sum(unlimited.trade_pcts[:k]) <= 0  # a prefix that fails the rule
    )
    _, ended = midas_clone(d, names, start="2010-06-01", **{**free, "trial_trades": n})
    assert ended.n_trades == n and ended.trial_ended != ""
    assert ended.trade_pcts == unlimited.trade_pcts[:n]  # same trades until the rule fires


def test_the_default_clone_is_unchanged_by_the_new_rules():
    d = _bundle(days=500)
    _, stats = midas_clone(d, d.universes["sp500"], start="2010-06-01")
    assert stats.trial_ended == "" and stats.n_trades > 0  # eight slots, next-open entries


# ---------- the account ----------


def test_off_trend_slices_earn_the_cash_rate_only_with_the_sleeve():
    falling = [200.0 - 0.1 * i for i in range(600)]
    d = _flat({t: falling for t in CORE})
    cfg = account.AccountConfig(core_allocation_pct=85.0, core_trend_window=200)
    swept = account.core_returns(d, cfg)
    idle = account.core_returns(
        d, account.AccountConfig(core_allocation_pct=85.0, cash_sleeve=False)
    )
    assert float((idle.abs() < 1e-12).all()) == 1.0  # nothing held, nothing earned
    year = swept.iloc[-252:]
    # 85% of the account at 4% a year; the 15% left for the swing book earns nothing
    assert (1 + year).prod() - 1 == pytest.approx(0.85 * 0.04, rel=0.03)


def test_on_trend_slices_hold_the_allocation_and_no_more():
    rising = [100.0 * 1.0005**i for i in range(600)]
    d = _flat({t: rising for t in CORE}, cash_rate=0.0)
    core = account.core_returns(d, account.AccountConfig(core_allocation_pct=85.0))
    held = core.iloc[-100:]
    # every slice on trend: 85% of the account in assets rising 5 bp a day
    assert held.mean() == pytest.approx(0.85 * 0.0005, rel=0.05)


def test_the_replay_is_the_two_books_added_up():
    d = _bundle()
    out = account.replay(d, account.AccountConfig(), start=str(d.index[300].date()))
    total, core, swing = out.returns["account"], out.returns["core"], out.returns["swing"]
    assert total.index.equals(core.index) and total.index[0] >= d.index[300]
    assert np.allclose(total.to_numpy(), (core + swing).to_numpy())
    table = out.table()
    assert list(table["book"])[:3] == [
        "Midas as configured (core + swing)",
        "Core book: 85% of equity, off-trend share in T-bills",
        "Swing book: 15% budget, the rules as code",
    ]
    assert {"Buy & hold SPY", "T-bills"} <= set(table["book"])
    assert list(out.by_year().columns) == ["year", "midas", "spy", "t_bills"]


def test_the_command_prints_the_replay(tmp_path, capsys):
    d = _bundle(days=700)
    path = tmp_path / "daily.pkl"
    d.to_pickle(path)
    assert research_main(["backtest", "--data", str(path), "--years", "1.5"]) == 0
    text = capsys.readouterr().out
    assert "Midas as configured (core + swing)" in text
    assert "What this does not replay" in text and "| year |" in text
    assert research_main(["backtest", "--data", str(path), "--years", "50"]) == 0  # all it has
