"""Research toolkit, daily side: data bundle, simulator, strategies,
metrics, walk-forward and the report. Everything runs on synthetic or
hand-built series with known answers."""

import numpy as np
import pandas as pd
import pytest

from midas.research import strategies
from midas.research.data import PriceData, cash_from_tbill_yield, synthetic
from midas.research.metrics import (
    cagr,
    calendar_year_returns,
    max_drawdown,
    sharpe,
    summarize,
)
from midas.research.portfolio import fixed_weights, monthly_calendar, on_signal_change, simulate
from midas.research.report import render
from midas.research.swing_clone import midas_clone
from midas.research.walkforward import walk_forward


def _bundle(close: pd.DataFrame, cash_rate: float = 0.0) -> PriceData:
    idx = close.index
    cash = pd.Series((1 + cash_rate) ** (1 / 252) - 1, index=idx, name="CASH")
    vol = pd.DataFrame(1e6, index=idx, columns=close.columns)
    return PriceData(
        open=close.shift(1).bfill(),
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=vol,
        cash_daily=cash,
    )


# ---------- data ----------


def test_synthetic_is_aligned_and_deterministic():
    a = synthetic(["A", "B"], days=300, seed=7)
    b = synthetic(["A", "B"], days=300, seed=7)
    assert a.close.equals(b.close)
    assert a.index.equals(a.cash_daily.index)
    assert a.tickers == ["A", "B"]
    assert len(a.since("2011-01-01").index) < len(a.index)


def test_misaligned_frames_are_rejected():
    d = synthetic(["A"], days=50)
    with pytest.raises(ValueError, match="aligned"):
        PriceData(
            open=d.open.iloc[1:],
            high=d.high,
            low=d.low,
            close=d.close,
            volume=d.volume,
            cash_daily=d.cash_daily,
        )


def test_cash_from_tbill_yield_compounds_to_the_quoted_rate():
    idx = pd.bdate_range("2020-01-01", periods=252)
    daily = cash_from_tbill_yield(pd.Series(5.0, index=idx), idx)
    assert float((1 + daily).prod()) == pytest.approx(1.05, rel=1e-9)


def test_pickle_round_trip(tmp_path):
    d = synthetic(["A", "B"], days=40)
    d.to_pickle(tmp_path / "x.pkl")
    back = PriceData.from_pickle(tmp_path / "x.pkl")
    assert back.close.equals(d.close) and back.cash_daily.equals(d.cash_daily)


# ---------- simulator ----------


def test_full_weight_with_zero_cost_reproduces_the_asset():
    d = synthetic(["A"], days=120, seed=1)
    w = pd.DataFrame({"A": [1.0]}, index=[d.index[10]])
    out = simulate(w, d.returns, d.cash_daily, cost_bps=0.0)
    expected = d.returns["A"].iloc[10:]
    assert out.index[0] == d.index[10]
    np.testing.assert_allclose(out.to_numpy(), expected.to_numpy(), atol=1e-12)


def test_turnover_cost_is_charged_once_on_the_rebalance_day():
    d = synthetic(["A"], days=60, seed=2)
    w = pd.DataFrame({"A": [1.0]}, index=[d.index[5]])
    out = simulate(w, d.returns, d.cash_daily, cost_bps=10.0)
    r = float(d.returns["A"].iloc[5])
    assert float(out.iloc[0]) == pytest.approx((1 - 0.001) * (1 + r) - 1, abs=1e-12)
    # afterwards the portfolio is the asset, no further cost
    np.testing.assert_allclose(
        out.iloc[1:].to_numpy(), d.returns["A"].iloc[6:].to_numpy(), atol=1e-12
    )


def test_unallocated_capital_earns_the_cash_rate():
    d = synthetic(["A"], days=60, seed=3, cash_rate=0.04)
    w = pd.DataFrame({"A": [0.0]}, index=[d.index[0]])
    out = simulate(w, d.returns, d.cash_daily, cost_bps=5.0)
    np.testing.assert_allclose(out.to_numpy(), d.cash_daily.to_numpy(), atol=1e-12)


def test_weights_are_long_only_and_capped_at_one():
    d = synthetic(["A", "B"], days=30, seed=4)
    w = pd.DataFrame({"A": [1.5], "B": [-0.5]}, index=[d.index[0]])
    out = simulate(w, d.returns, d.cash_daily, cost_bps=0.0)
    np.testing.assert_allclose(out.to_numpy(), d.returns["A"].fillna(0).to_numpy(), atol=1e-12)


def test_on_signal_change_keeps_only_flips():
    idx = pd.bdate_range("2020-01-01", periods=6)
    w = pd.DataFrame({"A": [1.0, 1.0, 0.0, 0.0, 1.0, 1.0]}, index=idx)
    kept = on_signal_change(w)
    assert list(kept.index) == [idx[0], idx[2], idx[4]]


def test_monthly_calendar_trades_the_first_day_after_month_end():
    idx = pd.bdate_range("2021-01-01", "2021-03-31")
    sig, reb = monthly_calendar(idx)
    assert list(sig[:2]) == [pd.Timestamp("2021-01-29"), pd.Timestamp("2021-02-26")]
    assert list(reb[:2]) == [pd.Timestamp("2021-02-01"), pd.Timestamp("2021-03-01")]
    assert len(sig) == len(reb)


def test_fixed_weights_waits_for_every_asset_to_exist():
    idx = pd.bdate_range("2021-01-01", "2021-04-30")
    close = pd.DataFrame({"A": 100.0, "B": np.nan}, index=idx)
    close.loc[idx >= "2021-03-01", "B"] = 50.0
    sig, reb = monthly_calendar(idx)
    w = fixed_weights(close, {"A": 0.5, "B": 0.5}, sig, reb)
    assert w.index.min() == pd.Timestamp("2021-04-01")  # B had no price at the Feb month-end


# ---------- strategies ----------


def _trend(days: int, slope: float, start: float = 100.0) -> pd.DataFrame:
    idx = pd.bdate_range("2015-01-01", periods=days)
    return pd.DataFrame({"SPY": start * (1 + slope) ** np.arange(days)}, index=idx)


def test_sma_trend_is_invested_in_an_uptrend_and_in_cash_in_a_downtrend():
    up = _bundle(_trend(300, 0.001))
    w = strategies.sma_trend(up, "SPY", window=50)
    assert set(w["SPY"].round(6)) == {1.0}
    down = _bundle(_trend(300, -0.001))
    w = strategies.sma_trend(down, "SPY", window=50)
    assert set(w["SPY"].round(6)) == {0.0}


def test_sma_trend_half_throttle_never_goes_fully_to_cash():
    close = pd.concat([_trend(200, 0.002), _trend(200, -0.003, start=150.0)])
    close.index = pd.bdate_range("2015-01-01", periods=400)
    w = strategies.sma_trend(_bundle(close), "SPY", window=50, below_weight=0.5)
    assert set(w["SPY"].round(6)) == {0.5, 1.0}


def test_sma_monthly_rebalances_monthly():
    w = strategies.sma_monthly(_bundle(_trend(600, 0.0005)), "SPY", window=100)
    assert len(w) > 10
    assert set(w["SPY"].round(6)) == {1.0}
    sig, reb = monthly_calendar(pd.DatetimeIndex(_trend(600, 0.0005).index))
    assert set(w.index) <= set(reb)


def test_dual_momentum_picks_the_leader_or_the_safe_asset():
    idx = pd.bdate_range("2015-01-01", periods=600)
    n = np.arange(600)
    close = pd.DataFrame(
        {"SPY": 100 * 1.0004**n, "EFA": 100 * 1.0010**n, "AGG": np.full(600, 50.0)}, index=idx
    )
    w = strategies.dual_momentum(_bundle(close, cash_rate=0.0))
    assert (w["EFA"] == 1.0).all() and (w["SPY"] == 0.0).all()
    falling = close.copy()
    falling["SPY"] = 100 * 0.999**n
    falling["EFA"] = 100 * 0.998**n
    w = strategies.dual_momentum(_bundle(falling, cash_rate=0.0))
    assert (w["AGG"] == 1.0).all()


def test_cross_sectional_holds_the_top_scores_equally():
    idx = pd.bdate_range("2015-01-01", periods=400)
    n = np.arange(400)
    close = pd.DataFrame(
        {"A": 100 * 1.002**n, "B": 100 * 1.001**n, "C": 100 * 0.999**n, "SPY": 100 * 1.001**n},
        index=idx,
    )
    w = strategies.cross_sectional(
        _bundle(close), ["A", "B", "C"], strategies.momentum_12_1, top=2, min_names=3
    )
    assert not w.empty
    assert (w["A"] == 0.5).all() and (w["B"] == 0.5).all() and (w["C"] == 0.0).all()


def test_cross_sectional_trend_filter_sends_the_book_to_cash():
    idx = pd.bdate_range("2015-01-01", periods=400)
    n = np.arange(400)
    close = pd.DataFrame(
        {"A": 100 * 1.002**n, "B": 100 * 1.001**n, "C": 100 * 0.999**n, "SPY": 100 * 0.998**n},
        index=idx,
    )
    w = strategies.cross_sectional(
        _bundle(close),
        ["A", "B", "C"],
        strategies.momentum_12_1,
        top=2,
        min_names=3,
        trend_filter=("SPY", 100),
    )
    assert (w.sum(axis=1) == 0.0).all()


def test_inverse_vol_weights_sum_to_one_and_favour_the_calm_asset():
    d = synthetic(["CALM", "WILD"], days=400, seed=5)
    close = d.close.copy()
    close["WILD"] = 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.03, 400)))
    close["CALM"] = 100 * np.exp(np.cumsum(np.random.default_rng(2).normal(0, 0.003, 400)))
    w = strategies.inverse_vol(_bundle(close), ["CALM", "WILD"])
    assert np.allclose(w.sum(axis=1), 1.0)
    assert (w["CALM"] > w["WILD"]).all()


# ---------- metrics ----------


def test_cagr_sharpe_and_drawdown_known_values():
    idx = pd.bdate_range("2020-01-01", periods=504)
    daily = (1.10) ** (1 / 252) - 1
    r = pd.Series(daily, index=idx)
    assert cagr(r) == pytest.approx(0.10, rel=1e-9)
    noisy = pd.Series(np.random.default_rng(0).normal(0.0005, 0.01, 504), index=idx)
    assert sharpe(noisy, noisy) == pytest.approx(0.0)  # excess over cash equal to itself
    assert sharpe(noisy) > 0.0  # against zero cash the drift is rewarded
    assert sharpe(r) != sharpe(r)  # constant returns: no volatility, Sharpe undefined
    path = pd.Series([0.10, -0.10, 0.05])  # 1.0 -> 1.1 -> 0.99 -> 1.0395
    assert max_drawdown(path) == pytest.approx(-0.10)


def test_calendar_year_returns_and_summary():
    idx = pd.bdate_range("2019-01-01", periods=600)
    r = pd.Series(0.0005, index=idx)
    yearly = calendar_year_returns(r)
    assert list(yearly.index)[:2] == [2019, 2020]
    perf = summarize(r, "flat", cash=pd.Series(0.0, index=idx))
    assert perf is not None and perf.pct_years_beat_cash == 1.0 and perf.max_drawdown == 0.0
    assert summarize(r.iloc[:100], "short") is None


# ---------- walk-forward ----------


def test_walk_forward_selects_on_trailing_window_and_reports_choices():
    d = synthetic(["SPY"], days=252 * 9, seed=11, annual_drift=0.10)
    res = walk_forward(
        d,
        lambda data, **kw: strategies.sma_trend(data, "SPY", **kw),
        [{"window": 50}, {"window": 200}],
        train_years=3,
        test_years=1,
    )
    assert not res.oos_returns.empty
    assert set(res.choices["chosen"]) <= {"window=50", "window=200"}
    # out-of-sample series starts after the first training window
    assert res.oos_returns.index[0].year >= d.index[0].year + 3
    assert set(res.candidates) == {"window=50", "window=200"}


# ---------- swing clone and report ----------


def test_swing_clone_runs_and_reports_trade_stats():
    d = synthetic(["SPY", "AAA", "BBB", "CCC"], days=900, seed=21, annual_vol=0.35)
    ret, stats = midas_clone(d, ["AAA", "BBB", "CCC"], start=str(d.index[300].date()))
    assert len(ret) > 100
    assert stats.n_trades > 0
    assert sum(stats.exits.values()) == stats.n_trades
    assert 0.0 <= stats.win_rate <= 1.0


def test_report_renders_markdown_from_a_bundle():
    d = synthetic(["SPY", "AGG"], days=252 * 7, seed=3)
    text = render(d, include_clone=False, windows=(str(d.index[-300].date()),))
    assert text.startswith("# Results")
    assert "Buy & hold SPY" in text and "60/40 SPY/AGG" in text
    assert "## Walk-forward check" in text
    assert "Midas swing skeleton" not in text  # no S&P universe in the bundle
