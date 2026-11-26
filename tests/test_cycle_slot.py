"""2026-11-27: no committee while the swing book has no room for its answer.

The daily cycle checked the weekday, the hour, the regime and the trial,
and never whether a new position could be taken. With one 13% position
inside a 15% budget, every committee still ran its paid research stages
and then met a risk check that could only refuse. Replaying the live rules
over ten years, about nine committees in ten ran in that state. The cycle
now stands aside until the book has room for at least the smallest
position, counting a thesis that is approved and waiting to fill as a
position already taken."""

import pytest

import midas.pipeline.screen as screen
from midas.config import settings
from midas.execution import paper_broker
from midas.models import Position, PositionStatus, ThesisStatus, TradeThesis
from midas.risk import engine
from midas.scheduler import monitor
from tests.test_stop_guard import _position


@pytest.fixture(autouse=True)
def _live_shape(monkeypatch):
    monkeypatch.setattr(settings, "auto_cycle_weekdays", "mon,tue,wed,thu,fri,sat,sun")
    monkeypatch.setattr(monitor, "_minutes_since_open", lambda: 75.0)
    monkeypatch.setattr(settings, "core_enabled", True)
    monkeypatch.setattr(settings, "core_allocation_pct", 85.0)  # the swing budget is 15%
    monkeypatch.setattr(settings, "max_notional_pct_per_trade", 13.0)
    monkeypatch.setattr(settings, "min_swing_notional_usd", 25.0)


@pytest.fixture()
def screens(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        screen, "run_screen", lambda f, r: calls.append("screen") or {"candidates": []}
    )
    return calls


def test_the_room_is_the_budget_less_what_is_held_and_what_is_waiting(db):
    paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
    assert engine.swing_room_usd(db, 1_000.0) == pytest.approx(150.0)
    position = _position(db, "NVDA", avg=130.0)  # one share: $130, the 13% slot
    assert engine.swing_room_usd(db, 1_000.0) == pytest.approx(20.0)
    position.status = PositionStatus.closed
    position.qty = 0.0
    thesis = db.get(TradeThesis, position.thesis_id)
    thesis.status = ThesisStatus.approved  # a yes that has not filled yet holds its slot
    db.flush()
    assert engine.swing_room_usd(db, 1_000.0) == pytest.approx(20.0)


def test_the_cycle_stands_aside_while_the_slot_is_taken(factory, demo_router, screens):
    with factory() as db:
        paper_broker.set_account(db, cash=870.0, equity=1_000.0)
        pid = _position(db, "NVDA", avg=130.0).id
        db.commit()
    assert monitor.maybe_auto_cycle(factory, demo_router) is None
    assert screens == []  # no screen, so no committee and no spend
    with factory() as db:  # the position closes: the same day's cycle can run
        position = db.get(Position, pid)
        position.status = PositionStatus.closed
        position.qty = 0.0
        db.commit()
    assert monitor.maybe_auto_cycle(factory, demo_router) is not None
    assert screens == ["screen"]


def test_a_thesis_waiting_to_fill_holds_the_slot(factory, demo_router, screens):
    with factory() as db:
        paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
        position = _position(db, "WFC", avg=85.0)
        position.status = PositionStatus.closed
        position.qty = 0.0
        db.get(TradeThesis, position.thesis_id).status = ThesisStatus.approved
        db.commit()
    assert monitor.maybe_auto_cycle(factory, demo_router) is None and screens == []


def test_an_empty_book_runs_the_cycle(factory, demo_router, screens):
    with factory() as db:
        paper_broker.set_account(db, cash=1_000.0, equity=1_000.0)
        db.commit()
    assert monitor.maybe_auto_cycle(factory, demo_router) is not None and screens == ["screen"]
