"""2026-11-13: the chart facts are computed in code, not labelled by a model.

On 2026-11-12 the screener labelled all fifteen deep-scan names
"Post-Earnings Drift" with one paragraph each; AXP had reported four weeks
earlier and had fallen three sessions running, and the committee argued
about a drift that was not there. The technical block now carries the
facts a setup is made of - the swing low, the 20-day high, the ATR, the
last volume gap, whether the pullback has been reclaimed, relative
strength - and names the setup itself."""

from datetime import date, timedelta

from midas.pipeline.committee import technical_block
from midas.pipeline.setup_gate import code_setup, facts_line

TODAY = date(2026, 11, 13)


def _bar(d: date, close: float, prev_close: float, *, volume: int = 1000, open_=None):
    """A bar whose range is always close +/- 1: the true range of any step of
    at most 1 is exactly 2, so the ATR of such a run is 2.0."""
    o = prev_close if open_ is None else open_
    return {
        "date": d.isoformat(),
        "open": round(o, 4),
        "high": round(close + 1, 4),
        "low": round(close - 1, 4),
        "close": round(close, 4),
        "volume": volume,
    }


def _run(closes, *, today: date = TODAY, volumes=None, opens=None) -> list[dict]:
    """Completed bars ending yesterday, one per calendar day."""
    bars = []
    n = len(closes)
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        bars.append(
            _bar(
                today - timedelta(days=n - i),
                c,
                prev,
                volume=(volumes or {}).get(i, 1000),
                open_=(opens or {}).get(i),
            )
        )
    return bars


def uptrend(n: int = 55, start: float = 100.0, step: float = 0.5) -> list[float]:
    return [start + step * i for i in range(n)]


def pullback_bars(today: date = TODAY) -> list[dict]:
    """55 sessions up by 0.5, then five down by 1.0: an uptrend pulling
    back 4.7% off its high, closing under its 10-day mean (the AXP shape of
    2026-11-11, where the committee bought the third down day)."""
    return _run(uptrend() + [126.0, 125.0, 124.0, 123.0, 122.0], today=today)


def reclaimed_bars(today: date = TODAY) -> list[dict]:
    return _run(uptrend() + [126.0, 125.0, 124.0, 123.0, 122.0, 126.5], today=today)


def crash_bars(today: date = TODAY) -> list[dict]:
    closes = uptrend()
    for _ in range(5):
        closes.append(round(closes[-1] * 0.95, 4))
    return _run(closes, today=today)


def gap_bars(today: date = TODAY, holds: bool = True) -> list[dict]:
    closes = uptrend()  # ends 127
    gap_open = 127.0 * 1.06
    if holds:
        after = [gap_open + 0.5 * k for k in range(1, 6)]
    else:
        after = [gap_open + 0.5] + [gap_open + 0.5 - k for k in range(1, 5)]
    closes += after
    return _run(closes, today=today, volumes={55: 3000}, opens={55: gap_open})


# ---------- the pullback: structure and the reclaim ----------


def test_a_falling_pullback_names_its_levels_and_has_no_reclaim():
    tech = technical_block(pullback_bars(), today=TODAY)
    assert tech["trend"] == "uptrend" and tech["setup"] == "pullback-in-uptrend"
    assert tech["pullback_from_20d_high_pct"] == 4.69
    assert tech["high_20d"] == {"price": 128.0, "date": (TODAY - timedelta(days=6)).isoformat()}
    assert tech["swing_low"] == {"price": 121.0, "date": (TODAY - timedelta(days=1)).isoformat()}
    assert tech["atr14"] == 2.0 and tech["atr14_pct"] == 1.64
    assert tech["prior_close"] == 123.0 and tech["prior_high"] == 124.0
    assert tech["reclaim"] is False and tech["reclaim_bar"] is False
    assert tech["setup_note"] == (
        "4.7% off the 20-day high in an uptrend; no reclaim yet (close 122 under its 10-day 125)"
    )
    assert tech["last_gap"] is None


def test_a_close_above_the_10_day_and_the_prior_close_is_a_reclaim():
    tech = technical_block(reclaimed_bars(), today=TODAY)
    assert tech["reclaim"] is True
    assert tech["reclaim_bar"] is True  # 126.5 closed above the prior bar's high of 124
    assert tech["setup"] == "pullback-in-uptrend"
    assert tech["setup_note"].endswith(
        "reclaimed (close 126.5 above its 10-day 125.15 and the prior close 122)"
    )


def test_a_steady_drift_up_counts_as_reclaimed():
    # every close above the 10-day mean and the prior close: nothing is falling
    tech = technical_block(_run(uptrend(60)), today=TODAY)
    assert tech["reclaim"] is True and tech["reclaim_bar"] is False
    assert tech["setup"] == "pullback-in-uptrend"


# ---------- no setup ----------


def test_a_crash_is_a_downtrend_with_no_setup():
    tech = technical_block(crash_bars(), today=TODAY)
    assert tech["trend"] == "downtrend" and tech["above_ma50"] is False
    assert tech["setup"] == "none"
    assert tech["setup_note"].startswith("below its 50-day average")


def test_an_extended_chart_has_no_setup():
    closes = uptrend() + [127.0 * 1.12]
    tech = technical_block(_run(closes), today=TODAY)
    assert tech["setup"] == "none" and tech["setup_note"].startswith("extended")


# ---------- the gap ----------


def test_an_up_gap_on_volume_that_holds_is_a_gap_and_hold():
    tech = technical_block(gap_bars(), today=TODAY)
    assert tech["last_gap"] == {
        "date": (TODAY - timedelta(days=5)).isoformat(),
        "gap_pct": 6.0,
        "volume_ratio": 3.0,
        "sessions_ago": 4,
        "direction": "up",
        "low": 134.12,
        "holds": True,
    }
    assert tech["setup"] == "gap-and-hold"
    assert tech["setup_note"] == (
        "+6.0% gap on 3.0x volume 4 session(s) ago; holding above the gap bar's low 134.12"
    )


def test_a_gap_that_gave_way_is_not_a_gap_and_hold():
    tech = technical_block(gap_bars(holds=False), today=TODAY)
    assert tech["last_gap"]["holds"] is False
    assert tech["setup"] == "pullback-in-uptrend"


# ---------- relative strength ----------


def test_relative_strength_needs_a_benchmark():
    tech = technical_block(pullback_bars(), today=TODAY)
    assert tech["rs_20d_vs_spy_pct"] is None
    flat = _run([100.0] * 60)
    tech = technical_block(pullback_bars(), today=TODAY, benchmark_bars=flat)
    assert tech["rs_20d_vs_spy_pct"] == 2.09  # the stock's 20-session change against a flat index
    short = _run([100.0] * 10)
    tech = technical_block(pullback_bars(), today=TODAY, benchmark_bars=short)
    assert tech["rs_20d_vs_spy_pct"] is None


# ---------- the screener's line ----------


def test_the_facts_line_is_what_the_screener_reads():
    tech = technical_block(pullback_bars(), today=TODAY, benchmark_bars=_run([100.0] * 60))
    assert facts_line("AXP", tech, score=99.4) == (
        "AXP 99: pullback-in-uptrend; 4.7% off the 20d high; no reclaim yet (close 122 under "
        "its 10-day 125); ATR 1.6%; RS20 vs SPY +2.1%"
    )
    assert facts_line("DUNE", technical_block(crash_bars(), today=TODAY)).startswith(
        "DUNE: none (below its 50-day average"
    )
    assert facts_line("X", None) == "X: technical state unknown"


def test_code_setup_is_pure_and_handles_a_thin_block():
    assert code_setup({"close": 10.0}) == ("none", "technical state incomplete")
    assert code_setup(None) == ("none", "technical state unknown")
