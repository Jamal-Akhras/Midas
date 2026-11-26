"""Text scores for the event studies: word-list tone, the point-in-time
rank, the signal day of a call, and the study that joins them to returns.

2026-11-27: the first text study (docs/studies/2026-11-earnings-call-tone.md)
asks whether the tone of an earnings call ranks the weeks after it. Every
piece that could leak the future has a test here: the signal day waits for
a session that had the call before its close, the rank sees only earlier
events, and the planted-effect test shows a move on the signal day itself
is not counted as something the score predicted."""

import numpy as np
import pandas as pd
import pytest

from midas.research import events, text
from midas.research.data import PriceData

POS = frozenset({"STRONG", "GROWTH", "RECORD"})
NEG = frozenset({"LOSS", "DECLINE", "WEAK", "IMPAIRMENT"})


# ---------- the score ----------


def test_tone_counts_whole_words_in_any_case():
    words = "Record growth this quarter; growth was strong. One loss, no declines."
    assert text.tone_counts(words, POS, NEG) == (4, 1)  # "declines" is not "decline"
    assert text.tone_counts("", POS, NEG) == (0, 0)


def test_tone_is_the_net_share_and_needs_enough_words():
    assert text.tone(30, 20, min_words=50) == pytest.approx(0.2)
    assert text.tone(0, 60, min_words=50) == -1.0
    assert text.tone(30, 19, min_words=50) is None  # 49 sentiment words: too few to score


# ---------- the signal day ----------


def test_the_signal_day_is_the_first_session_that_closed_after_the_call_began():
    sessions = pd.bdate_range("2024-01-01", periods=10)  # Mon 1 Jan .. Fri 12 Jan
    day = lambda s: text.signal_day(pd.Timestamp(s), sessions)  # noqa: E731
    assert day("2024-01-02 10:00") == pd.Timestamp("2024-01-02")  # during the session
    assert day("2024-01-02 08:30") == pd.Timestamp("2024-01-02")  # before the open
    assert day("2024-01-02 16:00") == pd.Timestamp("2024-01-03")  # at the close: next session
    assert day("2024-01-02 17:00") == pd.Timestamp("2024-01-03")
    assert day("2024-01-05 17:00") == pd.Timestamp("2024-01-08")  # Friday evening -> Monday
    assert day("2024-01-06 11:00") == pd.Timestamp("2024-01-08")  # a Saturday
    assert day("2024-01-02 00:00") == pd.Timestamp("2024-01-03")  # time unknown: wait a session
    assert day("2024-01-12 17:00") is None  # nothing after it in the data


# ---------- the change against the company's own calls ----------


def test_abnormal_tone_is_measured_against_the_previous_four_calls():
    frame = pd.DataFrame(
        {
            "symbol": ["A"] * 7 + ["B"],
            "signal_day": [*pd.bdate_range("2020-01-01", periods=7, freq="63B"), "2020-01-01"],
            "tone": [0.1, 0.2, 0.3, 0.6, 0.0, 0.0, 0.5, 0.9],
        }
    )
    frame["signal_day"] = pd.to_datetime(frame["signal_day"])
    out = text.abnormal_tone(frame)
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])  # fewer than two earlier calls
    assert out.iloc[2] == pytest.approx(0.3 - 0.15)
    assert out.iloc[3] == pytest.approx(0.6 - 0.2)
    assert out.iloc[6] == pytest.approx(0.5 - (0.3 + 0.6 + 0.0 + 0.0) / 4)  # the last four only
    assert np.isnan(out.iloc[7])  # another company starts its own history


# ---------- the point-in-time rank ----------


def test_the_rank_sees_only_events_before_the_signal_day():
    days = pd.to_datetime(
        ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-07", "2021-06-01"]
    )
    values = pd.Series([1.0, 2.0, 3.0, 2.5, 9.0, 0.0])
    rank = text.trailing_percentile(days, values, window_days=365, min_events=3)
    assert rank.iloc[:3].isna().all()  # fewer than three earlier events
    assert rank.iloc[3] == pytest.approx(2 / 3)  # above 1.0 and 2.0, below 3.0
    assert rank.iloc[4] == pytest.approx(1.0)  # its same-day neighbour is not in its pool
    assert np.isnan(rank.iloc[5])  # everything earlier is more than a year old
    later = values.copy()
    later.iloc[4] = -5.0  # a later or same-day event changes nothing before it
    again = text.trailing_percentile(days, later, window_days=365, min_events=3)
    assert again.iloc[3] == rank.iloc[3]


# ---------- returns against the same-day universe ----------


def test_universe_excess_is_the_return_minus_the_days_liquid_mean():
    idx = pd.bdate_range("2024-01-01", periods=2)
    raw = pd.DataFrame({"A": [3.0, 1.0], "B": [1.0, 1.0], "C": [50.0, np.nan]}, index=idx)
    liquid = pd.DataFrame({"A": True, "B": True, "C": False}, index=idx)
    out = events.universe_excess(raw, liquid)
    assert out.loc[idx[0], "A"] == pytest.approx(1.0)  # mean of the liquid names is 2.0
    assert out.loc[idx[0], "C"] == pytest.approx(48.0)  # measured against it, not part of it
    assert out.loc[idx[1], "A"] == pytest.approx(0.0)


def test_the_spread_is_tested_on_days_that_have_both_legs():
    idx = pd.bdate_range("2024-01-01", periods=40)
    exc = pd.DataFrame({"A": 1.0, "B": -1.0, "C": 0.0}, index=idx)
    exc.loc[idx[::2], "A"] += 0.5  # vary it so the t-statistic exists
    top = pd.DataFrame(False, index=idx, columns=exc.columns)
    bottom = top.copy()
    top.loc[idx[:30], "A"] = True
    bottom.loc[idx[10:], "B"] = True
    spread, t, days = text.spread_stat(exc, top, bottom, lags=0)
    assert days == 20  # sessions 10..29 have both
    assert spread == pytest.approx(2.25) and t > 3


# ---------- the study, on prices with a planted effect ----------


def _bundle(n_days: int = 900, n_names: int = 40, seed: int = 3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=n_days)
    names = [f"S{i:02d}" for i in range(n_names)]
    ret = pd.DataFrame(rng.normal(0.0, 0.005, size=(n_days, n_names)), index=idx, columns=names)
    return idx, names, ret, rng


def _prices(ret: pd.DataFrame) -> PriceData:
    close = 100.0 * (1.0 + ret).cumprod()
    close["SPY"] = 100.0
    open_ = close.shift(1).bfill()  # each session opens at the prior close
    return PriceData(
        open=open_,
        high=np.maximum(open_, close),
        low=np.minimum(open_, close),
        close=close,
        volume=pd.DataFrame(1e7, index=close.index, columns=close.columns),
        cash_daily=pd.Series(0.0, index=close.index, name="CASH"),
        universes={"sp500": [c for c in close.columns if c != "SPY"]},
    )


def _calls(idx, names, rng) -> pd.DataFrame:
    rows = []
    for j, name in enumerate(names):
        for k in range(60 + j % 4, len(idx) - 40, 63):  # a call a quarter, ten names a day
            rows.append({"symbol": name, "ts": idx[k] + pd.Timedelta(hours=10), "pos": 0, "neg": 0})
    calls = pd.DataFrame(rows)
    tone_value = rng.uniform(-0.6, 0.6, size=len(calls))
    calls["pos"] = np.round(100 * (1 + tone_value) / 2).astype(int)
    calls["neg"] = 100 - calls["pos"]
    return calls


def test_the_study_finds_a_planted_drift_and_the_placebo_does_not():
    idx, names, ret, rng = _bundle()
    calls = _calls(idx, names, rng)
    pos = {d: i for i, d in enumerate(idx)}
    for row in calls.itertuples():
        k = pos[row.ts.normalize()]
        tone_value = (row.pos - row.neg) / 100.0
        ret.iloc[k + 1 : k + 21, ret.columns.get_loc(row.symbol)] += (
            0.004 * tone_value
        )  # after entry
    study = text.earnings_tone_study(
        _prices(ret), calls, score="tone", start="2018-06-01", min_rank_events=50
    )
    long_leg = study.rows["top quintile"]
    assert long_leg.excess > 2.0 and long_leg.t_excess > 3  # drift near 3.8%
    assert study.rows["bottom quintile"].excess < -2.0
    assert study.spread > 4.0 and study.spread_t > 3
    assert abs(study.placebo.t_excess) < 2.5  # shuffled scores find nothing


def test_a_move_on_the_signal_day_itself_is_not_credited_to_the_score():
    idx, names, ret, rng = _bundle(seed=5)
    calls = _calls(idx, names, rng)
    pos = {d: i for i, d in enumerate(idx)}
    for row in calls.itertuples():
        k = pos[row.ts.normalize()]
        tone_value = (row.pos - row.neg) / 100.0
        ret.iloc[k, ret.columns.get_loc(row.symbol)] += 0.05 * tone_value  # the reaction, pre-entry
    study = text.earnings_tone_study(
        _prices(ret), calls, score="tone", start="2018-06-01", min_rank_events=50
    )
    assert abs(study.rows["top quintile"].t_excess) < 2.5
    assert abs(study.spread_t) < 2.5
    # the reaction is there to be seen, as the control variable
    assert study.events["reaction"].corr(study.events["score"]) > 0.5


# ---------- the pass mark ----------


def _row(excess: float, t: float) -> events.EventRow:
    return events.EventRow("x", 20, 1000, 500, excess - 0.2, excess, t, 52.0)


def _study(**kw) -> text.ToneStudy:
    base = dict(
        horizon=20,
        score="abnormal",
        benchmark="universe",
        events=pd.DataFrame(),
        rows={"top quintile": _row(0.60, 3.4)},
        recent={"top quintile": _row(0.50, 2.2)},
        spread=0.9,
        spread_t=2.8,
        spread_days=900,
        reaction_split=[
            ("weak reaction", 0.4, 1.0, 300),
            ("middle reaction", 0.5, 1.2, 300),
            ("strong reaction", 0.7, 1.5, 300),
        ],
        placebo=_row(0.05, 0.3),
    )
    return text.ToneStudy(**{**base, **kw})


def test_a_study_that_clears_every_criterion_passes():
    assert [v.passed for v in text.judge(_study())] == [True] * 5
    assert text.passed(_study())


def test_each_criterion_can_fail_the_study_on_its_own():
    fails = {
        0: _study(rows={"top quintile": _row(0.39, 5.0)}),  # too small, however certain
        1: _study(recent={"top quintile": _row(-0.50, -2.5)}),  # the sign flipped since 2020
        2: _study(spread_t=1.9),
        3: _study(
            reaction_split=[
                ("weak reaction", -0.1, 0, 300),
                ("middle reaction", 0.9, 1, 300),
                ("strong reaction", 0.9, 1, 300),
            ]
        ),
        4: _study(placebo=_row(0.6, 2.4)),  # noise clears the bar: the result is void
    }
    for k, study in fails.items():
        verdicts = text.judge(study)
        assert verdicts[k].passed is False and not text.passed(study), k
    assert text.judge(_study(rows={"top quintile": _row(0.60, 2.9)}))[0].passed is False
    half = _study(reaction_split=[("a", 0.1, 0, 1), ("b", 0.1, 0, 1), ("c", 0.1, 0, 1)])
    assert text.judge(half)[3].passed is False  # positive everywhere but far under half


def test_a_missing_statistic_is_a_fail_not_a_skip():
    empty = _study(
        rows={},
        recent={},
        spread=float("nan"),
        spread_t=float("nan"),
        reaction_split=[],
        placebo=None,
    )
    assert [v.passed for v in text.judge(empty)] == [False] * 5


def test_the_report_states_the_verdict_and_the_unjudged_rows():
    idx, names, ret, rng = _bundle()
    report = text.tone_report(
        _prices(ret), _calls(idx, names, rng), start="2018-06-01", min_rank_events=50
    )
    assert "Against the pass mark:" in report and "**Verdict: FAIL.**" in report  # pure noise
    assert "Reported, not judged:" in report and "price reaction alone" in report


def test_the_negative_score_is_a_density_not_tone_in_other_units():
    sessions = pd.bdate_range("2024-01-01", periods=10)
    calls = pd.DataFrame(
        {
            "symbol": ["A", "B"],
            "ts": pd.to_datetime(["2024-01-02 10:00", "2024-01-02 10:00"]),
            "pos": [60, 60],
            "neg": [40, 40],
            "chars": [10_000, 40_000],  # the same tone, said in a quarter of the words
        }
    )
    out = text.build_events(calls, sessions, ["A", "B"]).set_index("symbol")
    assert out.loc["A", "tone"] == out.loc["B", "tone"] == pytest.approx(0.2)
    assert out.loc["A", "negative"] == pytest.approx(-4.0)
    assert out.loc["B", "negative"] == pytest.approx(-1.0)
