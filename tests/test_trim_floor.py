"""2026-11-20: a small position is held or sold, not whittled.

The portfolio manager trimmed SNPS from $128 to $63 on 2026-11-16 and to
$30 on 2026-11-18, realising a loss each time and leaving a remainder too
small to be evidence of anything. The existing dust rule only refused a
remainder under MIN_SWING_NOTIONAL_USD ($25). A trim that would leave less
than TRIM_MIN_REMAINDER_USD is now refused in code, the reviewers are told
so up front (`trim_allowed`), and a refused trim is no longer logged as a
portfolio action."""

import pytest
from sqlalchemy import select

from midas.config import settings
from midas.execution import executor, paper_broker
from midas.models import AuditEvent, Order
from tests.test_execution_entry_gate import _open_position


@pytest.fixture(autouse=True)
def _floor(monkeypatch):
    monkeypatch.setattr(settings, "autonomy_level", 2)
    monkeypatch.setattr(settings, "fractional_shares", True)
    monkeypatch.setattr(settings, "min_swing_notional_usd", 25.0)
    monkeypatch.setattr(settings, "trim_min_remainder_usd", 75.0)


def test_a_trim_that_leaves_less_than_the_floor_is_refused(db, monkeypatch):
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 450.0)
    p = _open_position(db, "SNPS", qty=0.282, avg_price=450.0)  # $127: half is $63
    assert executor.trim_allowed(p) is False
    assert executor.propose_or_execute_exit(db, p, decision="trim", reasons=["half"]) is None
    assert p.qty == 0.282 and db.query(Order).count() == 0
    (note,) = [
        e.summary
        for e in db.scalars(select(AuditEvent).where(AuditEvent.event_type == "exit.trim_skipped"))
    ]
    assert note == (
        "SNPS: trim of 0.141 would leave 0.141 ($63.45), below the $75 floor - hold or exit instead"
    )


def test_a_position_large_enough_is_trimmed(db, monkeypatch):
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 100.0)
    p = _open_position(db, "BIG", qty=2.0, avg_price=100.0)  # half leaves $100
    assert executor.trim_allowed(p) is True
    order = executor.propose_or_execute_exit(db, p, decision="trim", reasons=["half"])
    assert order.qty == 1.0 and p.qty == 1.0


def test_an_exit_is_never_refused_by_the_trim_floor(db, monkeypatch):
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 450.0)
    p = _open_position(db, "SNPS", qty=0.282, avg_price=450.0)
    order = executor.propose_or_execute_exit(db, p, decision="exit", reasons=["thesis broken"])
    assert order.qty == 0.282


def test_the_floor_can_be_switched_off(db, monkeypatch):
    monkeypatch.setattr(settings, "trim_min_remainder_usd", 0.0)
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 450.0)
    p = _open_position(db, "SNPS", qty=0.282, avg_price=450.0)
    assert executor.trim_allowed(p) is True  # only the $25 dust rule is left
