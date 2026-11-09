"""Operator notifications: the twice-daily brief must be built from the ledger
alone, say loudly when the monitor has stopped reporting, and reach Telegram
without the bot token ever appearing in a log line. The Claude review is an
optional add-on: when the CLI is missing or slow the brief still goes out."""

import logging
import subprocess
import uuid
from datetime import timedelta

import httpx

from midas.audit import record_event
from midas.models import (
    ApprovalRequest,
    ArtifactType,
    AuditEvent,
    CostEvent,
    Direction,
    Instrument,
    PipelineRun,
    Position,
    PositionStatus,
    ResearchArtifact,
    RunKind,
    SystemState,
    ThesisStatus,
    TradeThesis,
    utcnow,
)
from midas.notify import brief as brief_mod
from midas.notify import review, telegram
from midas.notify.brief import build_brief, default_since, format_brief, mark_brief_sent


def _thesis(db, ticker):
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
    thesis = TradeThesis(
        run_id=run.id,
        verdict_artifact_id=art.id,
        ticker=ticker,
        direction=Direction.bullish,
        instrument=Instrument.shares,
        entry_zone_low=1,
        entry_zone_high=2,
        stop_price=0.5,
        status=ThesisStatus.executed,
    )
    db.add(thesis)
    db.flush()
    return thesis


def _seed(db, now, *, heartbeat_minutes_ago=5, next_pass_in_minutes=10):
    db.add(SystemState(key="paper_account", value={"cash": 760.16, "equity": 1002.21}))
    db.add(
        SystemState(
            key="equity_anchors",
            value={"day": now.date().isoformat(), "day_equity": 1000.0},
        )
    )
    db.add(
        SystemState(
            key="monitor_heartbeat",
            value={
                "at": (now - timedelta(minutes=heartbeat_minutes_ago)).isoformat(),
                "next_pass_at": (now + timedelta(minutes=next_pass_in_minutes)).isoformat(),
            },
        )
    )
    t = _thesis(db, "SPY")
    db.add(
        Position(
            thesis_id=t.id,
            ticker="SPY",
            symbol="SPY",
            instrument=Instrument.shares,
            qty=0.161,
            avg_price=744.048,
            stop_price=0.0,
            status=PositionStatus.open,
            book="core",
            last_mark_price=752.51,
            opened_at=now,
        )
    )
    record_event(db, event_type="run.completed", summary="TSLA: no_trade: bearish case won")
    record_event(db, event_type="run.completed", summary="Screen kept 5 candidate(s)")
    record_event(db, event_type="order.filled", summary="buy 0.161 SPY @ 744.048 (alpaca_paper)")
    record_event(db, event_type="execution.mismatch", summary="MIRROR MISMATCH EFA: 1 vs 2")
    record_event(db, event_type="ops.alert", summary="standing-stop cancellation unconfirmed")
    record_event(db, event_type="stage.completed", summary="noise")
    old = AuditEvent(event_type="ops.alert", summary="ancient alert, before the window")
    db.add(old)
    db.flush()
    old.created_at = now - timedelta(days=2)
    db.add(CostEvent(task_type="verdict", provider="openai", model="gpt-4o", cost_usd=0.05))
    stale_cost = CostEvent(task_type="verdict", provider="openai", model="gpt-4o", cost_usd=0.10)
    db.add(stale_cost)
    db.flush()
    stale_cost.created_at = now - timedelta(days=2)
    db.add(
        ApprovalRequest(
            subject_type="thesis", subject_id=uuid.uuid4(), title="EXIT X", summary="", context={}
        )
    )
    db.flush()


def test_brief_reads_the_ledger_window(db):
    now = utcnow()
    _seed(db, now)
    b = build_brief(db, since=now - timedelta(hours=12), now=now)

    assert b.cash == 760.16 and b.equity == 1002.21
    assert abs(b.day_change_pct - 0.221) < 0.01
    assert not b.heartbeat_stale
    assert [p["symbol"] for p in b.positions] == ["SPY"]
    assert abs(b.positions[0]["upnl"] - (752.51 - 744.048) * 0.161) < 1e-6
    assert b.decisions == ["TSLA: no_trade: bearish case won"]  # screens are not decisions
    assert b.trades == ["buy 0.161 SPY @ 744.048 (alpaca_paper)"]
    assert len(b.alerts) == 2 and all("ancient" not in a for a in b.alerts)
    assert b.pending_approvals == 1
    assert abs(b.spend_since_usd - 0.05) < 1e-9  # the two-day-old call is outside the window
    assert abs(b.spend_today_usd - 0.05) < 1e-9


def test_stale_heartbeat_leads_the_brief(db):
    now = utcnow()
    _seed(db, now, heartbeat_minutes_ago=120, next_pass_in_minutes=-100)
    b = build_brief(db, since=now - timedelta(hours=12), now=now)
    assert b.heartbeat_stale
    text = format_brief(b)
    assert text.splitlines()[0].startswith("WARNING: monitor")


def test_missing_heartbeat_counts_as_stale(db):
    b = build_brief(db, since=utcnow() - timedelta(hours=12), now=utcnow())
    assert b.heartbeat_stale and b.heartbeat_at is None


def test_format_is_plain_text_and_fits_a_telegram_message(db):
    now = utcnow()
    _seed(db, now)
    text = format_brief(build_brief(db, since=now - timedelta(hours=12), now=now))
    assert "Midas brief" in text
    assert "equity $1,002.21" in text and "+0.22%" in text
    assert "SPY 0.161" in text
    assert "TSLA: no_trade" in text
    assert "MIRROR MISMATCH" in text
    assert "1 approval" in text
    assert "*" not in text and "#" not in text  # no markdown: Telegram would show it raw
    assert len(text) <= telegram.MAX_MESSAGE_CHARS


def test_default_since_uses_the_last_sent_brief(db):
    now = utcnow()
    assert default_since(db, now) == now - timedelta(hours=24)
    mark_brief_sent(db, now - timedelta(hours=3))
    assert default_since(db, now) == now - timedelta(hours=3)


def test_send_message_posts_to_the_chat_and_never_logs_the_token(monkeypatch, caplog):
    posted: list[tuple[str, dict]] = []

    def fake_post(url, *, json=None, timeout=None):
        posted.append((url, json))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    with caplog.at_level(logging.DEBUG):
        ok = telegram.send_message("hello", token="123:SECRET", chat_id="42")
    assert ok
    url, payload = posted[0]
    assert url.endswith("/bot123:SECRET/sendMessage")
    assert payload["chat_id"] == "42" and payload["text"] == "hello"
    assert "SECRET" not in caplog.text


def test_send_message_failure_is_logged_without_the_url(monkeypatch, caplog):
    def boom(url, *, json=None, timeout=None):
        raise httpx.ConnectError("no route", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", boom)
    with caplog.at_level(logging.WARNING):
        ok = telegram.send_message("hello", token="123:SECRET", chat_id="42")
    assert not ok
    assert "ConnectError" in caplog.text and "SECRET" not in caplog.text


def test_unconfigured_sender_refuses_quietly(caplog):
    with caplog.at_level(logging.WARNING):
        assert not telegram.send_message("hello", token="", chat_id="42")
    assert "not configured" in caplog.text


def test_long_messages_are_split_on_line_breaks(monkeypatch):
    posted: list[str] = []

    def fake_post(url, *, json=None, timeout=None):
        posted.append(json["text"])
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
    assert telegram.send_message(text, token="t", chat_id="c")
    assert len(posted) > 1
    assert all(len(p) <= telegram.MAX_MESSAGE_CHARS for p in posted)
    assert "\n".join(posted) == text  # nothing lost, nothing cut mid-line


def test_discover_chat_returns_the_latest_private_chat(monkeypatch):
    updates = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 7, "type": "private", "first_name": "A"}}},
            {
                "update_id": 2,
                "message": {"chat": {"id": 99, "type": "private", "first_name": "Jamal"}},
            },
        ],
    }

    def fake_get(url, *, timeout=None):
        return httpx.Response(200, json=updates, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert telegram.discover_chat(token="t") == {"chat_id": "99", "name": "Jamal"}


def test_discover_chat_without_messages_is_none(monkeypatch):
    def fake_get(url, *, timeout=None):
        body = {"ok": True, "result": []}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert telegram.discover_chat(token="t") is None


def test_claude_review_feeds_the_report_on_stdin_with_tools_off(monkeypatch):
    calls: list[dict] = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": cmd, **kw})
        return subprocess.CompletedProcess(cmd, 0, stdout="  Nothing looks off.\n", stderr="")

    monkeypatch.setattr(review.shutil, "which", lambda name: r"C:\x\claude.EXE")
    monkeypatch.setattr(review.subprocess, "run", fake_run)
    out = review.claude_review("REPORT BODY", cli="claude", model="sonnet", timeout_s=30)
    assert out == "Nothing looks off."
    call = calls[0]
    assert call["cmd"][0] == r"C:\x\claude.EXE"
    assert "-p" in call["cmd"] and "--model" in call["cmd"] and "sonnet" in call["cmd"]
    assert call["cmd"][call["cmd"].index("--tools") + 1] == ""
    assert call["input"] == "REPORT BODY" and call["timeout"] == 30


def test_claude_review_degrades_to_none(monkeypatch, caplog):
    monkeypatch.setattr(review.shutil, "which", lambda name: None)
    with caplog.at_level(logging.WARNING):
        assert review.claude_review("r", cli="claude") is None
    assert "not found" in caplog.text

    monkeypatch.setattr(review.shutil, "which", lambda name: "claude")

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(review.subprocess, "run", slow)
    assert review.claude_review("r", cli="claude", timeout_s=1) is None

    def failed(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="auth required")

    monkeypatch.setattr(review.subprocess, "run", failed)
    assert review.claude_review("r", cli="claude") is None


def test_alert_types_cover_the_events_an_operator_must_see():
    for kind in ("execution.mismatch", "ops.alert", "pipeline.halted", "order.rejected"):
        assert kind in brief_mod.ALERT_TYPES


def test_telegram_rendering_is_readable_html(db):
    from datetime import datetime, timezone

    now = utcnow()
    _seed(db, now)
    record_event(db, event_type="ops.alert", summary="a <b>tag</b> & an ampersand")
    b = build_brief(db, since=now - timedelta(hours=12), now=now)
    b.as_of = datetime(2026, 11, 9, 17, 52, tzinfo=timezone.utc)
    html_text = brief_mod.render_telegram(
        b, "Nothing looks off.\nWatch the open.", tz=timezone(timedelta(hours=4))
    )
    lines = html_text.splitlines()

    assert lines[0] == "<b>Midas</b> \u00b7 Mon 09 Nov 21:52 UTC+4"  # the operator's clock
    assert lines.index("\U0001f9e0 <b>Review</b>") < lines.index("\U0001f4c8 <b>Positions</b>")
    assert "Nothing looks off." in lines and "Watch the open." in lines
    assert "\u2022 SPY 0.161 @ 744.05 \u2192 752.51 (+1.36)" in lines
    assert "a &lt;b&gt;tag&lt;/b&gt; &amp; an ampersand" in html_text  # ledger text is escaped
    assert "TSLA: no trade. bearish case won" in html_text
    assert all(line.count("<b>") == line.count("</b>") for line in lines)  # tags never span lines


def test_telegram_rendering_collapses_repeated_alerts_and_warns_first(db):
    now = utcnow()
    _seed(db, now, heartbeat_minutes_ago=120, next_pass_in_minutes=-100)
    record_event(db, event_type="execution.mismatch", summary="MIRROR MISMATCH EFA: 1 vs 2")
    b = build_brief(db, since=now - timedelta(hours=12), now=now)
    html_text = brief_mod.render_telegram(b, None)
    assert html_text.startswith("\U0001f6a8 <b>Monitor loop is not reporting</b>")
    assert "MIRROR MISMATCH EFA: 1 vs 2 (x2)" in html_text
    assert "<b>Alerts (3)</b>" in html_text  # the count is of events, the lines are collapsed
    assert "<b>Review</b>" not in html_text  # no review, no empty section


def test_send_message_passes_parse_mode_only_when_asked(monkeypatch):
    payloads: list[dict] = []

    def fake_post(url, *, json=None, timeout=None):
        payloads.append(json)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    telegram.send_message("a", token="t", chat_id="c")
    telegram.send_message("<b>a</b>", token="t", chat_id="c", parse_mode="HTML")
    assert "parse_mode" not in payloads[0] and payloads[1]["parse_mode"] == "HTML"


def test_send_rich_falls_back_to_plain_text(monkeypatch):
    payloads: list[dict] = []

    def fake_post(url, *, json=None, timeout=None):
        payloads.append(json)
        status = 400 if json.get("parse_mode") else 200
        return httpx.Response(
            status, json={"ok": status == 200}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    assert telegram.send_rich("<b>bad<i>", "plain words", token="t", chat_id="c")
    assert [p["text"] for p in payloads] == ["<b>bad<i>", "plain words"]
    assert "parse_mode" not in payloads[1]
