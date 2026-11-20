"""2026-11-20: the levels are the chart's, and the gate checks them first.

Ten of the fifteen committees run in the week of 2026-11-13 ended at the
verdict sanity gate: the investor's stop and target did not pay 1.5 to 1,
or the stop sat inside one ATR. Every number needed to know that was in
the technical block before the committee started. The block now carries
`levels` - the structural stop (under the swing low or the gap bar's low,
never nearer than MIN_STOP_ATR), the first target (the 20-day high) and
the reward-to-risk between them from the last close - and the setup gate
drops a pullback with no room, and any candidate with earnings inside the
entry window (HD and LOW reached committee zero and one day before their
reports)."""

from datetime import timedelta

import pytest

from midas.config import settings
from midas.pipeline.committee import technical_block
from midas.pipeline.setup_gate import facts_line, setup_gate, structure_levels
from tests.test_setup_facts import TODAY, reclaimed_bars


@pytest.fixture(autouse=True)
def _rules(monkeypatch):
    monkeypatch.setattr(settings, "min_stop_atr", 1.0)
    monkeypatch.setattr(settings, "min_reward_to_risk", 1.5)
    monkeypatch.setattr(settings, "setup_gate_require_room", True)
    monkeypatch.setattr(settings, "entry_earnings_blackout_days", 7)


def _tech(**kw) -> dict:
    base = {
        "close": 100.0,
        "ma10": 99.0,
        "ma50": 95.0,
        "above_ma50": True,
        "extension_vs_10d_pct": 1.0,
        "pullback_from_20d_high_pct": 5.7,
        "atr14": 2.0,
        "reclaim": True,
        "setup": "pullback-in-uptrend",
        "setup_note": "5.7% off the 20-day high in an uptrend; reclaimed",
        "swing_low": {"price": 97.0, "date": "2026-11-10"},
        "high_20d": {"price": 106.0, "date": "2026-11-03"},
    }
    return {**base, **kw}


# ---------- the levels ----------


def test_the_levels_are_the_swing_low_and_the_20_day_high():
    assert structure_levels(_tech()) == {
        "entry_ref": 100.0,
        "stop": 97.0,
        "stop_basis": "swing low",
        "stop_atr": 1.5,
        "target": 106.0,
        "target_basis": "20-day high",
        "reward_to_risk": 2.0,
    }


def test_a_swing_low_inside_one_atr_gives_way_to_the_atr():
    levels = structure_levels(_tech(swing_low={"price": 99.5, "date": "2026-11-12"}))
    assert levels["stop"] == 98.0 and levels["stop_basis"] == "1 ATR under the close"
    assert levels["stop_atr"] == 1.0 and levels["reward_to_risk"] == 3.0


def test_a_gap_and_hold_stops_under_the_gap_bar_and_may_have_no_target():
    gap = _tech(
        setup="gap-and-hold",
        last_gap={"low": 96.0, "direction": "up", "holds": True, "sessions_ago": 3},
        high_20d={"price": 100.0, "date": "2026-11-19"},  # at its high: nothing overhead
    )
    levels = structure_levels(gap)
    assert levels["stop"] == 96.0 and levels["stop_basis"] == "gap bar low"
    assert levels["target"] is None and levels["reward_to_risk"] is None


def test_no_setup_no_levels():
    assert structure_levels(_tech(setup="none")) is None
    assert structure_levels(_tech(atr14=None)) is None
    assert structure_levels(None) is None


def test_the_technical_block_carries_the_levels():
    tech = technical_block(reclaimed_bars(), today=TODAY)
    levels = tech["levels"]
    assert levels["entry_ref"] == tech["close"] and levels["target"] == tech["high_20d"]["price"]
    assert levels["stop"] <= tech["swing_low"]["price"]
    assert levels["stop_atr"] >= 1.0


# ---------- the gate: room ----------


def test_a_pullback_with_no_room_is_dropped():
    tech = _tech(high_20d={"price": 103.0, "date": "2026-11-03"})
    tech["levels"] = structure_levels(tech)
    assert setup_gate(tech) == [
        "no room: the 20-day high 103 is 1.00 times the risk from the close 100 with the "
        "stop at 97 (swing low; minimum 1.5) - the retest does not pay for the stop"
    ]


def test_a_pullback_with_room_passes():
    tech = _tech()
    tech["levels"] = structure_levels(tech)
    assert setup_gate(tech) == []


def test_the_room_rule_is_the_pullbacks_and_can_be_switched_off(monkeypatch):
    thin = _tech(high_20d={"price": 103.0, "date": "2026-11-03"})
    thin["levels"] = structure_levels(thin)
    monkeypatch.setattr(settings, "setup_gate_require_room", False)
    assert setup_gate(thin) == []
    monkeypatch.setattr(settings, "setup_gate_require_room", True)
    monkeypatch.setattr(settings, "min_reward_to_risk", 0.0)  # no floor: nothing to compare
    assert setup_gate(thin) == []
    monkeypatch.setattr(settings, "min_reward_to_risk", 1.5)
    gap = _tech(
        setup="gap-and-hold",
        last_gap={"low": 97.0, "direction": "up", "holds": True, "sessions_ago": 3},
        high_20d={"price": 101.0, "date": "2026-11-18"},
    )
    gap["levels"] = structure_levels(gap)
    assert setup_gate(gap) == []  # momentum at its highs has no overhead reference


# ---------- the gate: the earnings window ----------


def test_earnings_inside_the_entry_window_drops_the_candidate(monkeypatch):
    soon = (TODAY + timedelta(days=1)).isoformat()
    tech = _tech(days_to_earnings=1, next_earnings_date=soon)
    tech["levels"] = structure_levels(tech)
    assert setup_gate(tech) == [
        f"earnings in 1 day(s) ({soon}), inside the 7-day entry window - a swing entry here "
        "is a bet on the report"
    ]
    assert setup_gate({**tech, "days_to_earnings": 8}) == []
    assert setup_gate({**tech, "days_to_earnings": None}) == []
    monkeypatch.setattr(settings, "entry_earnings_blackout_days", 0)
    assert setup_gate(tech) == []


# ---------- the screener's line ----------


def test_the_facts_line_states_the_room():
    tech = _tech()
    tech["levels"] = structure_levels(tech)
    assert "; room 2.0R to the 20d high; ATR" in facts_line("ACME", {**tech, "atr14_pct": 2.0})
