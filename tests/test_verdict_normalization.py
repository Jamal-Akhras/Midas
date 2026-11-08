"""Model-output hygiene: synonym normalization and strict validation on
the verdict schema (bad values must trigger the repair loop, never crash
thesis creation)."""

import pytest
from pydantic import ValidationError

from midas.agents.schemas import VerdictOutput


def base(**kw):
    d = dict(
        ticker="ORCL",
        verdict="trade",
        direction="bullish",
        instrument="option",
        entry_zone_low=100.0,
        entry_zone_high=105.0,
        stop_price=92.0,
        targets=[120.0],
        thesis_summary="x",
        what_invalidates_this=["stop"],
    )
    d.update(kw)
    return d


def test_long_short_normalize_to_bullish_bearish():
    assert VerdictOutput(**base(direction="long")).direction == "bullish"
    assert VerdictOutput(**base(direction="Short")).direction == "bearish"


def test_instrument_synonyms_normalize():
    assert VerdictOutput(**base(instrument="options")).instrument == "option"
    assert VerdictOutput(**base(instrument="Stock")).instrument == "shares"


def test_unknown_direction_rejected_for_repair():
    with pytest.raises(ValidationError, match="bullish"):
        VerdictOutput(**base(direction="sideways"))


def test_no_trade_untouched():
    v = VerdictOutput(
        ticker="ORCL", verdict="no_trade", thesis_summary="x", no_trade_reason="too risky"
    )
    assert v.verdict == "no_trade"
