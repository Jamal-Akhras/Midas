"""Verdict and research-schema rules added after the 2026-11-10 review:

- "No Trade" is normalised before validation (every AAPL verdict spent a
  repair call on capitalisation);
- a no_trade that carries an entry zone is a wait-for-pullback, which IS a
  trade verdict (AAPL 11-10: no_trade with entry 340-345 at spot 349.65 was
  thrown away although the executor rests a limit for 5 sessions);
- a past "next earnings date" is the LAST report, not the next: it goes to
  unverified instead of being planned around."""

import json
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from midas.agents.schemas import CatalystOutput, FundamentalsOutput, VerdictOutput
from midas.providers.router import ModelRouter
from tests.conftest import FakeProvider

TODAY = date.today()


def _trade(**kw) -> dict:
    d = dict(
        ticker="AAPL",
        verdict="trade",
        direction="bullish",
        instrument="shares",
        entry_zone_low=340.0,
        entry_zone_high=345.0,
        stop_price=330.0,
        targets=[365.0],
        thesis_summary="pullback entry",
        what_invalidates_this=["close below 330"],
    )
    d.update(kw)
    return d


# ---------- verdict string normalisation ----------


def test_verdict_string_is_normalised_before_validation():
    v = VerdictOutput(ticker="AAPL", verdict="No Trade", thesis_summary="x", no_trade_reason="r")
    assert v.verdict == "no_trade"
    assert VerdictOutput(**_trade(verdict=" TRADE ")).verdict == "trade"
    assert VerdictOutput(**_trade(verdict="Trade")).verdict == "trade"


def test_unknown_verdict_still_rejected():
    with pytest.raises(ValidationError, match="trade"):
        VerdictOutput(ticker="AAPL", verdict="maybe", thesis_summary="x", no_trade_reason="r")


# ---------- wait-for-pullback is a trade ----------


def test_no_trade_with_entry_zone_is_rejected_for_repair():
    with pytest.raises(ValidationError, match="wait-for-pullback is a trade verdict"):
        VerdictOutput(
            ticker="AAPL",
            verdict="no_trade",
            entry_zone_low=340.0,
            entry_zone_high=345.0,
            thesis_summary="wait for 340-345",
            no_trade_reason="price extended above ideal entry zone",
        )


def test_plain_no_trade_is_fine():
    v = VerdictOutput(ticker="AAPL", verdict="no_trade", thesis_summary="x", no_trade_reason="r")
    assert v.entry_zone_low is None and v.verdict == "no_trade"


def test_repair_prompt_converts_the_wait_for_pullback(db):
    """The router's single repair attempt carries the validation message, and
    the corrected answer validates - one extra call, never a crash."""
    bad = json.dumps(
        {
            "ticker": "AAPL",
            "verdict": "No Trade",
            "entry_zone_low": 340,
            "entry_zone_high": 345,
            "thesis_summary": "wait",
            "no_trade_reason": "extended",
        }
    )
    good = json.dumps(_trade())
    provider = FakeProvider("openai", [bad, good])
    router = ModelRouter(providers={"openai": provider})
    out = router.run(
        db,
        task_type="verdict",
        output_schema=VerdictOutput,
        system_prompt="s",
        user_prompt="u",
        risk_level="high",
    )
    assert out.verdict == "trade" and out.entry_zone_high == 345.0
    assert len(provider.calls) == 2  # first answer + ONE repair
    repair_messages = provider.calls[1]["messages"]
    assert any(
        m.get("role") == "user" and "wait-for-pullback is a trade verdict" in str(m.get("content"))
        for m in repair_messages
    )


# ---------- past earnings dates ----------


def test_past_next_earnings_date_moves_to_unverified():
    past = (TODAY - timedelta(days=8)).isoformat()
    f = FundamentalsOutput(ticker="GOOGL", recent_quarters=[], next_earnings_date=past)
    assert f.next_earnings_date == ""
    assert any(past in u and "past" in u for u in f.unverified)


def test_future_next_earnings_date_is_kept():
    nxt = (TODAY + timedelta(days=40)).isoformat()
    f = FundamentalsOutput(ticker="GOOGL", recent_quarters=[], next_earnings_date=nxt)
    assert f.next_earnings_date == nxt and f.unverified == []


def test_unparseable_next_earnings_date_is_not_a_fact():
    f = FundamentalsOutput(ticker="GOOGL", recent_quarters=[], next_earnings_date="late October")
    assert f.next_earnings_date == "" and f.unverified


def test_past_catalyst_earnings_date_is_dropped():
    past = (TODAY - timedelta(days=13)).isoformat()
    c = CatalystOutput(
        ticker="GOOGL",
        upcoming_earnings_date=past,
        catalysts=[
            {"event": "Upcoming Earnings Report", "date": past, "expected_impact": "high"},
            {"event": "Adobe collaboration", "date": past, "expected_impact": "medium"},
        ],
    )
    assert c.upcoming_earnings_date == ""
    assert [x.event for x in c.catalysts] == [
        "Adobe collaboration"
    ]  # news with a past date is fine
    assert any("past" in u for u in c.unverified)
