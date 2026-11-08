"""Intraday rules on hand-built sessions with known outcomes."""

import numpy as np
import pandas as pd
import pytest

from midas.research import intraday
from midas.research.cli import main as research_main
from midas.research.data import synthetic
from midas.research.intraday import (
    BreakoutParams,
    intraday_momentum,
    opening_range_breakout,
    overnight_intraday_split,
    session_bars,
)


def _session(
    day: str, closes: list[float], *, first_open=100.0, first_low=None, first_high=None
) -> pd.DataFrame:
    """78 five-minute bars 09:30-15:55 (exchange time). Bar i opens at the
    previous close; highs/lows hug the path unless overridden for bar 0."""
    assert len(closes) == 78
    idx = pd.date_range(f"{day} 09:30", periods=78, freq="5min", tz="America/New_York")
    opens = [first_open, *closes[:-1]]
    highs = [max(o, c) for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) for o, c in zip(opens, closes, strict=True)]
    if first_low is not None:
        lows[0] = first_low
    if first_high is not None:
        highs[0] = first_high
    return pd.DataFrame({"o": opens, "h": highs, "l": lows, "c": closes, "v": 1000}, index=idx)


def _flat_then(level: float, n_flat: int, rest: list[float]) -> list[float]:
    return [level] * n_flat + rest


def test_session_bars_keeps_the_regular_session_and_labels_half_hours():
    idx = pd.date_range("2024-03-04 09:00", periods=90, freq="5min", tz="America/New_York")
    raw = pd.DataFrame({"o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1}, index=idx)
    bars = session_bars(raw)
    assert bars.index.min().strftime("%H:%M") == "09:30"
    assert bars.index.max().strftime("%H:%M") == "15:55"
    assert bars["slot"].min() == 0 and bars["slot"].max() == 12
    assert bars["slot"].nunique() == 13


def test_first_candle_breakout_long_runs_to_the_close():
    # first candle 100 -> 101 (low 99.5); the day then grinds up to 104
    path = [101.0] + list(np.linspace(101.2, 104.0, 77))
    bars = session_bars(_session("2024-03-04", path, first_low=99.5))
    res = opening_range_breakout(bars, BreakoutParams(slippage_bps=0.0, sizing="flat"))
    row = res.iloc[0]
    shares = int(25_000 / 101.0)
    assert row["traded"] == 1
    assert row["ret"] == pytest.approx((104.0 - 101.0) * shares / 25_000)
    assert row["r"] == pytest.approx((104.0 - 101.0) / (101.0 - 99.5))


def test_first_candle_breakout_is_stopped_at_minus_one_r():
    path = [101.0, 100.8, 100.2, 99.0] + [99.5] * 74  # bar 3 trades through the stop at 99.5
    bars = session_bars(_session("2024-03-05", path, first_low=99.5))
    res = opening_range_breakout(bars, BreakoutParams(slippage_bps=0.0, sizing="flat"))
    assert res.iloc[0]["r"] == pytest.approx(-1.0)
    assert res.iloc[0]["ret"] < 0


def test_first_candle_breakout_gap_through_stop_exits_at_the_open():
    # bar 2 gaps open at 98, below the 99.5 stop: the exit is the open, worse than -1R
    path = [101.0, 100.6, 98.0] + [98.0] * 75
    raw = _session("2024-03-06", path, first_low=99.5)
    raw.iloc[2, raw.columns.get_loc("o")] = 98.0
    raw.iloc[2, raw.columns.get_loc("l")] = 98.0
    bars = session_bars(raw)
    res = opening_range_breakout(bars, BreakoutParams(slippage_bps=0.0, sizing="flat"))
    assert res.iloc[0]["r"] == pytest.approx((98.0 - 101.0) / (101.0 - 99.5))


def test_risk_sizing_caps_notional_at_max_leverage():
    path = [101.0] + list(np.linspace(101.2, 104.0, 77))
    bars = session_bars(_session("2024-03-07", path, first_low=100.9))  # tiny risk per share
    res = opening_range_breakout(
        bars, BreakoutParams(slippage_bps=0.0, sizing="risk", max_leverage=4.0)
    )
    shares_cap = int(25_000 * 4.0 / 101.0)
    assert res.iloc[0]["ret"] == pytest.approx((104.0 - 101.0) * shares_cap / 25_000)


def test_slippage_reduces_the_return_on_both_sides():
    path = [101.0] + list(np.linspace(101.2, 104.0, 77))
    bars = session_bars(_session("2024-03-08", path, first_low=99.5))
    clean = opening_range_breakout(bars, BreakoutParams(slippage_bps=0.0, sizing="flat")).iloc[0][
        "ret"
    ]
    slipped = opening_range_breakout(bars, BreakoutParams(slippage_bps=5.0, sizing="flat")).iloc[0][
        "ret"
    ]
    assert slipped < clean


def test_range_breakout_enters_on_the_first_close_outside_the_range():
    # 30-minute range 99.4-100.6, bar 6 closes at 102 -> long at 102, stop at the midpoint 100
    path = [100.6, 99.4, 100.2, 99.8, 100.4, 100.0, 102.0] + list(np.linspace(102.5, 105.0, 71))
    bars = session_bars(_session("2024-03-11", path, first_open=100.0))
    res = opening_range_breakout(
        bars, BreakoutParams(slippage_bps=0.0, sizing="flat"), range_bars=6
    )
    assert res.iloc[0]["r"] == pytest.approx((105.0 - 102.0) / (102.0 - 100.0))


def test_no_trade_when_the_first_candle_is_a_doji_or_the_range_never_breaks():
    flat = [100.0] * 78
    bars = session_bars(_session("2024-03-12", flat))
    assert opening_range_breakout(bars).iloc[0]["traded"] == 0
    assert opening_range_breakout(bars, range_bars=6).iloc[0]["traded"] == 0


def test_intraday_momentum_follows_the_first_half_hour():
    # first half hour (6 bars) up, last half hour (6 bars) up: a win net of two sides of cost
    path = list(np.linspace(100.5, 102.0, 6)) + [102.0] * 66 + list(np.linspace(102.5, 104.0, 6))
    bars = session_bars(_session("2024-03-13", path, first_open=100.0))
    res = intraday_momentum(bars, cost_bps=1.0)
    row = res.iloc[0]
    assert row["signal"] == 1.0
    assert row["ret"] == pytest.approx(row["r13"] - 2e-4)
    down = list(np.linspace(99.5, 98.0, 6)) + [98.0] * 66 + list(np.linspace(98.5, 100.0, 6))
    res = intraday_momentum(session_bars(_session("2024-03-14", down, first_open=100.0)))
    assert res.iloc[0]["signal"] == -1.0 and res.iloc[0]["ret"] < 0


def test_intraday_momentum_skips_incomplete_sessions():
    idx = pd.date_range("2024-03-15 09:30", periods=30, freq="5min", tz="America/New_York")
    raw = pd.DataFrame({"o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1}, index=idx)
    assert intraday_momentum(session_bars(raw)).empty


def test_overnight_intraday_split_is_an_exact_decomposition():
    d = synthetic(["X"], days=50, seed=9)
    split = overnight_intraday_split(d.open["X"], d.close["X"])
    recon = (1 + split["intraday"]) * (1 + split["overnight"]) - 1
    np.testing.assert_allclose(recon.to_numpy(), split["total"].to_numpy(), atol=1e-12)


def test_cli_report_writes_the_results_file(tmp_path):
    d = synthetic(["SPY", "AGG"], days=252 * 7, seed=4)
    d.to_pickle(tmp_path / "daily.pkl")
    out = tmp_path / "results.md"
    code = research_main(
        [
            "report",
            "--data",
            str(tmp_path / "daily.pkl"),
            "--out",
            str(out),
            "--no-clone",
            "--intraday",
            str(tmp_path / "missing"),
        ]
    )
    assert code == 0
    assert out.exists() and "# Results" in out.read_text(encoding="utf-8")


def test_cached_bars_round_trip(tmp_path):
    bars = _session("2024-03-18", [100.0] * 78)
    assert intraday.cached_bars(tmp_path, "SPY") is None
    intraday.cache_bars(tmp_path, "SPY", bars)
    back = intraday.cached_bars(tmp_path, "SPY")
    assert back is not None and len(back) == 78
