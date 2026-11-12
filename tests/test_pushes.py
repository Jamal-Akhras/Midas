"""2026-11-12: the operator hears about fills, stops and flags when they
happen, and the brief says when a mark sits under a stop.

Nothing reached Telegram between the 09:00 brief and the 19:05 brief on
the day NVDA was flagged premarket and stopped out at the open. Now each
monitor pass ends with one message listing that pass's pushable events,
and the brief flags a position marked below its stop (the 09:00 brief had
shown exactly that and said "Alerts: none")."""

from datetime import timedelta

from sqlalchemy import select

from midas.audit import record_event
from midas.config import settings
from midas.execution import paper_broker
from midas.models import (
    CORE_BOOK,
    SWING_BOOK,
    ActorType,
    ArtifactType,
    AuditEvent,
    Instrument,
    Position,
    ResearchArtifact,
    ThesisStatus,
    TradeThesis,
    utcnow,
)
from midas.models.domain import Direction, PipelineRun, RunKind
from midas.notify import push, telegram
from midas.notify.brief import build_brief
from midas.scheduler import monitor


def _sent(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def _fake(text, *, token, chat_id, timeout=15.0, parse_mode=None):
        calls.append({"text": text, "token": token, "chat_id": chat_id})
        return True

    monkeypatch.setattr(telegram, "send_message", _fake)
    return calls


def _position(db, ticker, *, stop, mark, book=SWING_BOOK):
    run = PipelineRun(kind=RunKind.committee, ticker=ticker)
    db.add(run)
    db.flush()
    art = ResearchArtifact(
        run_id=run.id,
        ticker=ticker,
        agent_slug="investor",
        artifact_type=ArtifactType.verdict,
        content={},
    )
    db.add(art)
    db.flush()
    th = TradeThesis(
        run_id=run.id,
        verdict_artifact_id=art.id,
        ticker=ticker,
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=100.0,
        entry_zone_high=100.0,
        stop_price=stop,
        targets=[120.0],
        status=ThesisStatus.executed,
        book=book,
    )
    db.add(th)
    db.flush()
    p = Position(
        thesis_id=th.id,
        ticker=ticker,
        symbol=ticker,
        instrument=Instrument.shares,
        qty=1.0,
        avg_price=100.0,
        stop_price=stop,
        last_mark_price=mark,
        opened_at=utcnow() - timedelta(days=1),
        book=book,
    )
    db.add(p)
    db.flush()
    return p


# ---------- the push ----------


def test_push_sends_this_passes_events_as_one_message(db, monkeypatch):
    calls = _sent(monkeypatch)
    old = utcnow() - timedelta(minutes=20)
    earlier = record_event(db, event_type="order.filled", summary="buy 0.46 AXP @ 322.99")
    earlier.created_at = old
    db.flush()
    since = utcnow()
    record_event(
        db,
        event_type="stop.breach_exit",
        summary="NVDA: underlying 155.6 breached stop 159.43 - deterministic exit",
        actor_type=ActorType.system,
    )
    record_event(db, event_type="order.filled", summary="sell 0.792 NVDA @ 155.61 (alpaca_paper)")
    record_event(db, event_type="stage.completed", summary="postmortem produced post_mortem v1")
    db.flush()
    n = push.push_pass_events(db, since, token="test-token", chat_id="42")
    assert n == 2
    (call,) = calls
    assert call["chat_id"] == "42"
    lines = call["text"].split("\n")
    assert lines[0].startswith("Midas ") and lines[0].endswith(" UTC")
    assert lines[1] == "- STOP: NVDA: underlying 155.6 breached stop 159.43 - deterministic exit"
    assert lines[2] == "- FILL: sell 0.792 NVDA @ 155.61 (alpaca_paper)"
    assert "AXP" not in call["text"] and "postmortem" not in call["text"]


def test_push_is_silent_when_nothing_happened_or_nothing_is_configured(db, monkeypatch):
    calls = _sent(monkeypatch)
    assert push.push_pass_events(db, utcnow(), token="t", chat_id="c") == 0
    record_event(db, event_type="order.filled", summary="buy 1 ACME @ 10")
    db.flush()
    assert push.push_pass_events(db, utcnow() - timedelta(minutes=1), token="", chat_id="") == 0
    assert calls == []


def test_a_long_pass_is_truncated(db):
    for i in range(15):
        record_event(db, event_type="order.filled", summary=f"fill {i}")
    db.flush()
    text = push.format_push(push.pass_events(db, utcnow() - timedelta(minutes=1)))
    assert text.count("\n- FILL") == push.MAX_LINES
    assert text.endswith("- and 3 more in the ledger")


def test_monitor_pass_pushes_its_stop_exit(factory, demo_router, monkeypatch):
    calls = _sent(monkeypatch)
    monkeypatch.setattr(settings, "autonomy_level", 2)
    monkeypatch.setattr(settings, "regime_filter_enabled", False)
    monkeypatch.setattr(settings, "core_enabled", False)
    monkeypatch.setattr(settings, "telegram_push_events", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_chat_id", "42")
    monkeypatch.setattr(monitor, "maybe_portfolio_review", lambda f, r: None)
    with factory() as db:
        paper_broker.set_account(db, cash=900.0, equity=1000.0)
        _position(db, "NVDA", stop=95.0, mark=100.0)
        db.commit()
    monkeypatch.setattr(monitor, "_underlying_price", lambda db, p: 90.0)  # under the floor
    monkeypatch.setattr(paper_broker, "mark_price", lambda db, o: 90.0)
    import midas.tools.market_data as md

    class _Q:
        data = {"price": 90.0, "sector": "Tech"}

    monkeypatch.setattr(md, "get_quote", lambda t: _Q())
    summary = monitor.run_monitor_once(factory, demo_router)
    assert summary["pushed"] == 3  # the L2 clock starting, the stop exit and its fill
    (call,) = calls
    assert "- L2 CLOCK: L2 evidence clock started" in call["text"]
    assert "- STOP: NVDA: underlying 90.0 breached disaster floor" in call["text"]
    assert "- FILL: sell 1 NVDA @ 90" in call["text"]
    assert call["token"] == "test-token"


def test_push_can_be_switched_off(factory, monkeypatch):
    calls = _sent(monkeypatch)
    monkeypatch.setattr(settings, "core_enabled", False)  # the live .env must not reach the test
    monkeypatch.setattr(settings, "regime_filter_enabled", False)
    monkeypatch.setattr(settings, "telegram_push_events", False)
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_chat_id", "42")
    with factory() as db:
        paper_broker.set_account(db, cash=900.0, equity=1000.0)
        db.commit()
    summary = monitor.run_monitor_once(factory, None)
    assert "pushed" not in summary and calls == []


# ---------- the brief ----------


def test_brief_flags_a_mark_below_the_stop(db):
    paper_broker.set_account(db, cash=750.0, equity=993.0)
    _position(db, "NVDA", stop=159.43, mark=158.78)
    _position(db, "AXP", stop=303.53, mark=322.0)
    _position(db, "EFA", stop=0.0, mark=104.0, book=CORE_BOOK)
    db.flush()
    brief = build_brief(db)
    assert brief.alerts == [
        "NVDA is marked below its stop (158.78 vs 159.43) on the latest print - "
        "the stop layer decides at the open"
    ]
    assert all(e.event_type != "x" for e in db.scalars(select(AuditEvent)))  # no event written
