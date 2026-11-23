"""Event studies: forward returns, the t-statistic, the signals and the
report section, on hand-built series with known answers.

2026-11-23: nine sessions of live decisions suggested four rule changes
(drop the reclaim gate, gate entries on the index, buy the dip, trade the
gap-and-hold). midas.research.events puts each to every signal day in the
data set, beside a baseline and a mirror, so docs/results.md carries the
answer instead of a memory of it."""

import numpy as np
import pandas as pd
import pytest

from midas.research import events
from midas.research.data import PriceData, synthetic
from midas.research.report import catalogue, render


def _bundle(close: pd.DataFrame, *, open_: pd.DataFrame | None = None, volume=None) -> PriceData:
    idx = close.index
    return PriceData(
        open=open_ if open_ is not None else close.shift(1).bfill(),
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=volume
        if volume is not None
        else pd.DataFrame(1e6, index=idx, columns=close.columns),
        cash_daily=pd.Series(0.0, index=idx, name="CASH"),
    )


def _days(n: int) -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-01", periods=n)


# ---------- the t-statistic ----------


def test_newey_west_with_no_lags_is_the_ordinary_t():
    x = np.array([1.0, 2.0, 0.5, 1.5, 3.0, -1.0, 2.5, 0.0])
    plain = x.mean() / (x.std() / np.sqrt(len(x)))
    assert events.newey_west_t(x, 0) == pytest.approx(plain)


def test_overlapping_windows_shrink_the_t():
    # each value repeated five times: what a 5-session forward return looks like
    x = np.repeat(np.array([2.0, -1.0, 3.0, 0.5, 1.5, -0.5, 2.5, 1.0]), 5)
    assert 0 < events.newey_west_t(x, 5) < events.newey_west_t(x, 0)


def test_too_short_or_constant_series_have_no_t():
    assert np.isnan(events.newey_west_t([1.0, 2.0, 3.0], 5))
    assert np.isnan(events.newey_west_t([1.0] * 20, 2))


# ---------- forward returns ----------


def test_forward_returns_buy_the_next_open_and_sell_the_close_h_later():
    idx = _days(6)
    close = pd.DataFrame(
        {"A": [100.0, 101, 102, 103, 104, 105], "SPY": [50.0, 50, 50, 50, 50, 51]}, index=idx
    )
    raw, excess = events.forward_returns(_bundle(close), ["A"], 2)
    # signal on day 0: buy the day-1 open (the day-0 close, 100), sell the day-3 close (103)
    assert raw["A"].iloc[0] == pytest.approx(3.0)
    assert excess["A"].iloc[0] == pytest.approx(3.0)  # SPY flat over that window
    # signal on day 2: 102 -> 105 is 2.94%; SPY 50 -> 51 is 2%
    assert raw["A"].iloc[2] == pytest.approx(2.941, abs=1e-3)
    assert excess["A"].iloc[2] == pytest.approx(0.941, abs=1e-3)
    assert raw["A"].iloc[3:].isna().all()  # no outcome yet


def test_an_event_row_averages_days_first_and_charges_the_cost():
    idx = _days(8)
    close = pd.DataFrame(
        {
            "A": [100.0, 101, 102, 103, 104, 105, 106, 107],
            "B": [100.0, 100, 100, 100, 100, 100, 100, 100],
            "SPY": [50.0] * 8,
        },
        index=idx,
    )
    raw, excess = events.forward_returns(_bundle(close), ["A", "B"], 2)
    mask = pd.DataFrame(False, index=idx, columns=["A", "B"])
    mask.loc[idx[0], ["A", "B"]] = True  # day 0: A +3.00%, B 0.00% -> day mean 1.50%
    mask.loc[idx[1], "A"] = True  # day 1: A 101 -> 104 = +2.97%
    row = events.event_row("test", mask, raw, excess, 2, cost_pct=0.2)
    assert (row.signals, row.days, row.horizon) == (3, 2, 2)
    assert row.raw_net == pytest.approx((1.5 + 2.9703) / 2 - 0.2, abs=0.01)
    assert row.excess == pytest.approx((1.5 + 2.9703) / 2, abs=0.01)
    assert row.win_net == pytest.approx(200 / 3, abs=0.1)  # B loses the cost
    # a window with no signal is no row, not a row of zeros
    assert events.event_row("none", mask, raw, excess, 2, start="2030-01-01") is None
    assert events.event_row("late", mask, raw, excess, 2, start=str(idx[1].date())).signals == 1


# ---------- signals ----------


def test_reclaim_needs_the_ten_day_mean_and_the_prior_close():
    close = pd.DataFrame({"A": [100.0] * 10 + [101.0, 100.5, 99.0]}, index=_days(13))
    out = events.reclaim(close)["A"]
    assert bool(out.iloc[10]) is True  # above the mean and the prior close
    assert bool(out.iloc[11]) is False  # above the mean, below the prior close
    assert bool(out.iloc[12]) is False


def test_rsi_is_100_after_a_rise_and_0_after_a_fall():
    idx = _days(12)
    close = pd.DataFrame(
        {"UP": np.arange(100.0, 112.0), "DOWN": np.arange(112.0, 100.0, -1.0)}, index=idx
    )
    out = events.rsi(close)
    assert out["UP"].iloc[-1] == pytest.approx(100.0)
    assert out["DOWN"].iloc[-1] == pytest.approx(0.0)
    assert np.isnan(out["UP"].iloc[0])  # no change to read on the first bar


def test_a_pullback_setup_is_off_the_high_inside_an_uptrend():
    closes = [100.0 + i for i in range(60)] + [155.0, 153.0]
    close = pd.DataFrame({"A": closes}, index=_days(len(closes)))
    out = events.pullback_setup(_bundle(close), ["A"])["A"]
    assert bool(out.iloc[59]) is False  # at its high: nothing has pulled back
    assert bool(out.iloc[61]) is True  # 4.7% off the 20-day high, above the 50-day
    falling = pd.DataFrame({"A": [200.0 - i for i in range(62)]}, index=_days(62))
    assert not events.pullback_setup(_bundle(falling), ["A"])["A"].any()  # no uptrend


def test_a_volume_gap_needs_the_gap_and_the_volume():
    idx = _days(25)
    close = pd.DataFrame({"A": [100.0] * 25, "B": [100.0] * 25}, index=idx)
    open_ = close.copy()
    open_.iloc[22] = [104.0, 104.0]  # both open 4% above the prior close
    volume = pd.DataFrame(1e6, index=idx, columns=["A", "B"])
    volume.loc[idx[22], "A"] = 2e6  # only A on twice its usual volume
    gap, heavy = events.volume_gap(_bundle(close, open_=open_, volume=volume), ["A", "B"])
    assert gap["A"].iloc[22] == pytest.approx(4.0)
    assert bool(heavy["A"].iloc[22]) is True and bool(heavy["B"].iloc[22]) is False


# ---------- the report ----------


def _sp_bundle(n: int = 100) -> PriceData:
    names = [f"S{i:03d}" for i in range(n)]
    d = synthetic(["SPY", *names], days=700, seed=11)
    return PriceData(
        open=d.open,
        high=d.high,
        low=d.low,
        close=d.close,
        volume=d.volume * 100,  # liquid enough to pass the dollar-volume floor
        cash_daily=d.cash_daily,
        universes={"sp500": names},
    )


def test_the_four_studies_come_back_as_tables():
    d = _sp_bundle(20)
    out = events.entry_rule_studies(d, d.universes["sp500"], recent="2012-01-02")
    assert set(out) == {"reclaim", "index", "dip", "gap"}
    cols = ["name", "horizon", "signals", "days", "raw_net", "excess", "t_excess", "win_net"]
    assert list(out["reclaim"].columns) == cols
    names = list(out["reclaim"]["name"])
    assert names[0] == "uptrend, any day (baseline)"
    assert "pullback setup, not reclaimed (the gate drops)" in names
    assert set(out["reclaim"]["horizon"]) == {10} and set(out["dip"]["horizon"]) == {5}
    # the gate splits the setup: the two halves never share a signal
    passed = out["reclaim"].set_index("name")["signals"]
    assert passed["pullback setup, reclaimed (the gate passes)"] > 0


def test_the_report_carries_the_event_studies():
    d = _sp_bundle(100)
    text = render(d, include_clone=False, windows=(str(d.index[-300].date()),))
    assert "## Entry rules as event studies" in text
    assert "The reclaim gate" in text and "pullback setup, reclaimed (the gate passes)" in text
    assert "The gap-and-hold beside its mirrors" in text  # synthetic bars never gap: no rows
    line = next(ln for ln in text.splitlines() if ln.startswith("| uptrend, any day (baseline) |"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    assert cells[4][0] in "+-" and len(cells[4].split(".")[1]) == 2  # raw_net: signed, 2 dp
    assert len(cells[6].split(".")[1]) == 1  # t_excess: 1 dp
    small = _sp_bundle(20)
    assert "## Entry rules as event studies" not in render(small, include_clone=False, windows=())
    assert "## Entry rules as event studies" not in render(
        d, include_clone=False, include_events=False, windows=()
    )


def test_the_catalogue_measures_the_core_share_and_the_cash_sleeve():
    d = synthetic(["SPY", "QQQ", "EFA", "GLD", "IEF"], days=252 * 4, seed=5, cash_rate=0.04)
    rows = {e.name: e.returns for e in catalogue(d)}
    idle = rows["Midas core 60%, idle cash at zero"]
    swept = rows["Midas core 60%, off-trend share and the rest in T-bills"]
    assert "Midas core 85%, off-trend share and the rest in T-bills" in rows
    assert "Midas core 100%, off-trend share in T-bills" in rows
    # the same slices on the same days: the sleeve can only add the cash rate
    assert (swept - idle).min() >= -1e-12
    assert (1 + swept).prod() > (1 + idle).prod()
