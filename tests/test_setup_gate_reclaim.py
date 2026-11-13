"""2026-11-13: the gate reads the code's own setup label.

The screener's label used to be the thing the gate checked; now the
technical block names the setup itself (`setup`), and the gate adds the
entry trigger: a pullback that has not closed back above its 10-day mean
and the prior close is still falling, and a chart with no code-defined
setup has nothing to gate into."""

import pytest

from midas.config import settings
from midas.pipeline.setup_gate import setup_gate
from midas.scheduler import monitor

PFE = {
    "close": 29.36,
    "ma10": 29.924,
    "ma20": 29.8455,
    "extension_vs_10d_pct": -1.88,
    "pullback_from_20d_high_pct": 6.62,
    "change_5d_pct": -2.13,
    "volume_5d_over_20d": 1.26,
    "above_ma50": True,
}


@pytest.fixture(autouse=True)
def _gate(monkeypatch):
    monkeypatch.setattr(settings, "setup_gate_enabled", True)
    monkeypatch.setattr(settings, "setup_gate_require_reclaim", True)
    monkeypatch.setattr(settings, "setup_gate_max_volume_ratio", 1.3)


def test_a_pullback_without_a_reclaim_is_gated(monkeypatch):
    block = {**PFE, "setup": "pullback-in-uptrend", "reclaim": False, "prior_close": 29.5}
    (failure,) = setup_gate(block, "breakout-pullback")
    assert failure == (
        "no reclaim yet: close 29.36 is under its 10-day mean 29.924 - the pullback is "
        "still falling; a setup needs a close above the 10-day and the prior close"
    )
    assert setup_gate({**block, "reclaim": True}, "breakout-pullback") == []
    monkeypatch.setattr(settings, "setup_gate_require_reclaim", False)
    assert setup_gate(block, "breakout-pullback") == []


def test_a_close_above_the_10_day_but_under_the_prior_close_is_named_as_such():
    block = {
        **PFE,
        "close": 30.0,
        "setup": "pullback-in-uptrend",
        "reclaim": False,
        "prior_close": 30.4,
    }
    (failure,) = setup_gate(block, "")
    assert failure.startswith("no reclaim yet: close 30 is under the prior close 30.4")


def test_a_chart_with_no_code_setup_is_gated():
    block = {**PFE, "setup": "none", "setup_note": "extended 9.2% above its 10-day mean"}
    assert setup_gate(block, "fundamentals-momentum") == [
        "no code-defined setup (extended 9.2% above its 10-day mean)"
    ]


def test_the_code_label_outranks_the_screener_label():
    block = {**PFE, "setup": "pullback-in-uptrend", "reclaim": True, "volume_5d_over_20d": 1.8}
    (failure,) = setup_gate(block, "post-earnings-drift")
    assert "volume 1.80x" in failure


def test_a_block_without_a_code_label_still_uses_the_screener_label():
    assert setup_gate({**PFE, "volume_5d_over_20d": 1.8}, "post-earnings-drift") == []
    assert len(setup_gate({**PFE, "volume_5d_over_20d": 1.8}, "breakout-pullback")) == 1


def test_the_reclaim_rule_is_part_of_the_strategy_fingerprint(monkeypatch):
    before = monitor._strategy_fingerprint()
    monkeypatch.setattr(settings, "setup_gate_require_reclaim", False)
    assert monitor._strategy_fingerprint() != before
