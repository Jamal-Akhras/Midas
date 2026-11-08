"""Live-path plumbing that must work offline: OCC symbol parsing, chain
assembly from Alpaca snapshot payloads, and mirror fallback behavior."""

from midas.config import settings
from midas.tools.market_data import _chain_from_snapshots, _occ_meta


def test_occ_symbol_parsing():
    meta = _occ_meta("ORCL260918C00150000")
    assert meta == {"expiry": "2026-09-18", "type": "call", "strike": 150.0}
    meta = _occ_meta("RKLB260821P00075500")
    assert meta == {"expiry": "2026-08-21", "type": "put", "strike": 75.5}
    assert _occ_meta("not-an-occ-symbol") is None


def test_chain_from_snapshots_builds_rows():
    from datetime import date, timedelta

    expiry = date.today() + timedelta(days=45)
    occ = f"ORCL{expiry.strftime('%y%m%d')}C00150000"
    snapshots = {
        occ: {
            "latestQuote": {"bp": 4.10, "ap": 4.30},
            "impliedVolatility": 0.31,
            "greeks": {"delta": 0.52},
            "openInterest": 1200,
            "dailyBar": {"v": 320},
        },
        "GARBAGE": {"latestQuote": {}},
    }
    chain = _chain_from_snapshots(snapshots, spot=150.0)
    assert len(chain) == 1
    row = chain[0]
    assert row["strike"] == 150.0 and row["type"] == "call"
    assert row["bid"] == 4.10 and row["ask"] == 4.30
    assert row["open_interest"] == 1200 and row["delta"] == 0.52
    assert 40 <= row["dte"] <= 50


def test_chain_filters_far_strikes_and_stale_expiries():
    from datetime import date, timedelta

    near = (date.today() + timedelta(days=30)).strftime("%y%m%d")
    snapshots = {
        f"ORCL{near}C00300000": {"latestQuote": {"bp": 1, "ap": 2}},  # +100% strike
        f"ORCL{near}C00150000": {"latestQuote": {"bp": 1, "ap": 2}},  # keep
        "ORCL200101C00150000": {"latestQuote": {"bp": 1, "ap": 2}},  # expired
    }
    chain = _chain_from_snapshots(snapshots, spot=150.0)
    assert len(chain) == 1


def test_mirror_disabled_without_keys(monkeypatch):
    import uuid

    from midas.execution.paper_broker import _mirror_to_alpaca
    from midas.models import Instrument, Order, OrderReason, OrderSide

    monkeypatch.setattr(settings, "alpaca_api_key", "")
    monkeypatch.setattr(settings, "alpaca_api_secret", "")
    order = Order(
        thesis_id=uuid.uuid4(),
        side=OrderSide.buy,
        instrument=Instrument.shares,
        symbol="ORCL",
        qty=5,
        reason=OrderReason.entry,
    )
    assert _mirror_to_alpaca(order) is None


def test_mirror_never_touches_options(monkeypatch):
    import uuid

    from midas.execution.paper_broker import _mirror_to_alpaca
    from midas.models import Instrument, Order, OrderReason, OrderSide

    monkeypatch.setattr(settings, "alpaca_api_key", "k")
    monkeypatch.setattr(settings, "alpaca_api_secret", "s")
    order = Order(
        thesis_id=uuid.uuid4(),
        side=OrderSide.buy,
        instrument=Instrument.option,
        symbol="ORCL_2026-09-18_call_150.0",
        qty=1,
        reason=OrderReason.entry,
    )
    assert _mirror_to_alpaca(order) is None  # options stay on the simulator
