"""Structurer hard checks: liquidity, DTE, spreads, premium caps - and the
rule that an LLM pick off the code-generated candidate list is rejected."""

from midas.risk import structurer_checks as sc
from midas.tools.demo_data import QUOTES, options_chain


def test_filter_prefers_configured_dte_window():
    chain = options_chain("ACME")
    legal = sc.filter_chain(chain, "call")
    assert legal, "demo chain must yield legal contracts"
    assert all(30 <= c.dte <= 60 for c in legal)


def test_illiquid_and_short_dte_rejected():
    chain = [
        {
            "expiry": "2026-08-01",
            "dte": 5,
            "type": "call",
            "strike": 100,
            "bid": 1.0,
            "ask": 1.1,
            "mid": 1.05,
            "open_interest": 5000,
            "volume": 100,
        },
        {
            "expiry": "2026-09-01",
            "dte": 40,
            "type": "call",
            "strike": 100,
            "bid": 1.0,
            "ask": 1.1,
            "mid": 1.05,
            "open_interest": 10,
            "volume": 1,
        },
        {
            "expiry": "2026-09-01",
            "dte": 40,
            "type": "call",
            "strike": 105,
            "bid": 0.0,
            "ask": 0.4,
            "mid": 0.2,
            "open_interest": 5000,
            "volume": 10,
        },
        {
            "expiry": "2026-09-01",
            "dte": 40,
            "type": "call",
            "strike": 110,
            "bid": 1.0,
            "ask": 2.0,
            "mid": 1.5,
            "open_interest": 5000,
            "volume": 10,
        },
    ]
    assert sc.filter_chain(chain, "call") == []  # dte, OI, no bid, wide spread


def test_candidates_are_defined_risk_and_capped():
    chain = options_chain("ACME")
    spot = QUOTES["ACME"]["price"]
    cands = sc.candidate_structures(
        chain, direction="bullish", spot=spot, expected_move_pct=12.0, account_equity=100_000
    )
    assert cands
    for c in cands:
        assert c["strategy"] in ("long_call", "call_debit_spread")
        assert c["est_premium_per_contract"] * 100 <= 100_000 * 0.02
        assert c["dte"] >= 14


def test_bearish_uses_puts():
    chain = options_chain("ACME")
    spot = QUOTES["ACME"]["price"]
    cands = sc.candidate_structures(
        chain, direction="bearish", spot=spot, expected_move_pct=-9.0, account_equity=100_000
    )
    assert cands
    assert all("put" in c["strategy"] for c in cands)


def test_off_list_pick_is_rejected():
    chain = options_chain("ACME")
    spot = QUOTES["ACME"]["price"]
    cands = sc.candidate_structures(
        chain, direction="bullish", spot=spot, expected_move_pct=12.0, account_equity=100_000
    )
    ok = sc.validate_pick(dict(cands[0]), cands)
    assert ok is not None
    tampered = {**cands[0], "long_strike": cands[0]["long_strike"] + 500}
    assert sc.validate_pick(tampered, cands) is None
