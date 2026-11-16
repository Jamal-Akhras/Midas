"""2026-11-16: exact earnings dates, past and future, from the calendar.

The earnings tool gave quarter ends, not report dates, so the chart facts
inferred an event day from the tape and the entry window read a date a
model had written. Now a code tool asks Finnhub's calendar for the last
report and the next one; the technical block carries both as days, the
gap that followed a report is named post-earnings, and the entry window
and the review trigger prefer the code-sourced date over the agents'."""

from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from midas.config import settings
from midas.models import ArtifactType, Instrument, Position, ResearchArtifact
from midas.models.domain import PipelineRun, RunKind
from midas.pipeline import earnings
from midas.pipeline.committee import run_committee, technical_block
from midas.pipeline.setup_gate import facts_line
from midas.scheduler import monitor
from midas.tools import demo_data
from midas.tools import fundamentals_news as fn
from midas.tools.registry import TOOLS
from tests.test_setup_facts import TODAY, gap_bars, pullback_bars

# ---------- the tool ----------


@pytest.fixture()
def live_finnhub(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "finnhub_api_key", "fh-test")
    monkeypatch.setattr(settings, "fmp_api_key", "")


def _fake_calendar(monkeypatch, events: list[dict]) -> list[dict]:
    calls: list[dict] = []

    def fake_get(url, *, params=None, timeout=None, headers=None):
        assert url.endswith("/calendar/earnings"), url
        calls.append(dict(params or {}))
        req = httpx.Request("GET", url)
        return httpx.Response(200, json={"earningsCalendar": events}, request=req)

    monkeypatch.setattr(fn.httpx, "get", fake_get)
    return calls


def test_the_calendar_gives_the_last_report_and_the_next(live_finnhub, monkeypatch):
    calls = _fake_calendar(
        monkeypatch,
        [
            {"date": "2026-07-17", "symbol": "AXP"},
            {"date": "2026-10-17", "symbol": "AXP"},
            {"date": "2027-01-28", "symbol": "AXP"},
            {"date": "2027-04-22", "symbol": "AXP"},
        ],
    )
    result = fn.get_earnings_dates("axp", today=date(2026, 11, 16))
    assert result.source == "finnhub"
    assert result.data == {
        "ticker": "AXP",
        "last_report_date": "2026-10-17",
        "next_report_date": "2027-01-28",
        "as_of": "2026-11-16",
    }
    (params,) = calls
    assert params["symbol"] == "AXP" and params["token"] == "fh-test"
    assert params["from"] == "2026-04-30" and params["to"] == "2027-03-16"  # 200 back, 120 ahead


def test_a_report_today_is_the_next_one(live_finnhub, monkeypatch):
    _fake_calendar(monkeypatch, [{"date": "2026-11-16"}, {"date": "2026-08-10"}])
    data = fn.get_earnings_dates("AXP", today=date(2026, 11, 16)).data
    assert data["last_report_date"] == "2026-08-10" and data["next_report_date"] == "2026-11-16"


def test_no_key_means_unknown_not_an_error(live_finnhub, monkeypatch):
    monkeypatch.setattr(settings, "finnhub_api_key", "")
    result = fn.get_earnings_dates("AXP", today=date(2026, 11, 16))
    assert result.source == "none"
    assert result.data["last_report_date"] is None and result.data["next_report_date"] is None


def test_the_demo_fixture_has_both_dates(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)  # never the live calendar for ACME
    data = fn.get_earnings_dates("ACME").data
    assert data["last_report_date"] == (demo_data.TODAY - timedelta(days=25)).isoformat()
    assert data["next_report_date"] == (demo_data.TODAY + timedelta(days=32)).isoformat()
    assert "get_earnings_dates" in TOOLS  # code calls it through the registry, for the snapshot


# ---------- the technical block ----------


def test_the_block_carries_the_dates_as_days():
    dates = {"last_report_date": "2026-10-17", "next_report_date": "2027-01-28"}
    tech = technical_block(pullback_bars(), today=TODAY, earnings=dates)
    assert tech["last_earnings_date"] == "2026-10-17" and tech["days_since_earnings"] == 27
    assert tech["next_earnings_date"] == "2027-01-28" and tech["days_to_earnings"] == 76
    bare = technical_block(pullback_bars(), today=TODAY)
    assert bare["days_since_earnings"] is None and bare["next_earnings_date"] is None


def test_a_gap_right_after_a_report_is_post_earnings():
    gap_day = TODAY - timedelta(days=5)
    reported = (gap_day - timedelta(days=1)).isoformat()  # reported after the close
    tech = technical_block(gap_bars(), today=TODAY, earnings={"last_report_date": reported})
    assert tech["last_gap"]["earnings"] is True
    assert tech["setup"] == "gap-and-hold"
    assert tech["setup_note"].startswith("+6.0% post-earnings gap on 3.0x volume")
    old = (gap_day - timedelta(days=20)).isoformat()
    tech = technical_block(gap_bars(), today=TODAY, earnings={"last_report_date": old})
    assert tech["last_gap"]["earnings"] is False
    assert tech["setup_note"].startswith("+6.0% gap on 3.0x volume")


def test_the_facts_line_names_the_dates_that_matter():
    tech = technical_block(
        pullback_bars(),
        today=TODAY,
        earnings={"last_report_date": "2026-11-09", "next_report_date": "2026-11-20"},
    )
    line = facts_line("AXP", tech)
    assert line.endswith("ATR 1.6%; reported 4 day(s) ago; earnings in 7 day(s)")
    far = technical_block(
        pullback_bars(),
        today=TODAY,
        earnings={"last_report_date": "2026-09-01", "next_report_date": "2027-01-28"},
    )
    assert "reported" not in facts_line("AXP", far) and "earnings in" not in facts_line("AXP", far)


# ---------- the entry window and the review trigger prefer the code's date ----------


def _artifact(db, run, slug, art_type, content, ticker="AXP"):
    art = ResearchArtifact(
        run_id=run.id, ticker=ticker, agent_slug=slug, artifact_type=art_type, content=content
    )
    db.add(art)
    db.flush()
    return art


def test_days_away_prefers_the_screen_contexts_code_date(db):
    run = PipelineRun(kind=RunKind.committee, ticker="AXP")
    db.add(run)
    db.flush()
    today = date(2026, 11, 16)
    _artifact(db, run, "catalyst", ArtifactType.catalysts, {"upcoming_earnings_date": "2026-11-18"})
    assert earnings.days_away(db, run.id, today=today) == (2, "2026-11-18")
    _artifact(
        db,
        run,
        "screen-context",
        ArtifactType.screen_context,
        {"technical": {"next_earnings_date": "2026-11-20"}},
    )
    assert earnings.days_away(db, run.id, today=today) == (4, "2026-11-20")
    # a screen context without a code date falls through to the agents' date
    run2 = PipelineRun(kind=RunKind.committee, ticker="AXP")
    db.add(run2)
    db.flush()
    _artifact(db, run2, "screen-context", ArtifactType.screen_context, {"technical": None})
    _artifact(
        db, run2, "catalyst", ArtifactType.catalysts, {"upcoming_earnings_date": "2026-11-18"}
    )
    assert earnings.days_away(db, run2.id, today=today) == (2, "2026-11-18")


def test_the_review_trigger_reads_the_code_date_first(db, monkeypatch):
    monkeypatch.setattr(settings, "earnings_lookahead_days", 7)
    run = PipelineRun(kind=RunKind.committee, ticker="AXP")
    db.add(run)
    db.flush()
    soon = (date.today() + timedelta(days=3)).isoformat()
    later = (date.today() + timedelta(days=30)).isoformat()
    _artifact(db, run, "catalyst", ArtifactType.catalysts, {"upcoming_earnings_date": later})
    p = Position(
        thesis_id=None,
        ticker="AXP",
        symbol="AXP",
        instrument=Instrument.shares,
        qty=1.0,
        avg_price=100.0,
        stop_price=90.0,
        last_mark_price=100.0,
    )
    assert monitor._earnings_proximity(db, p) is None  # 30 days out: not inside the window
    _artifact(
        db,
        run,
        "screen-context",
        ArtifactType.screen_context,
        {"technical": {"next_earnings_date": soon}},
    )
    assert monitor._earnings_proximity(db, p) == f"earnings on {soon} (3 day(s) away)"


# ---------- the screen and the committee carry them ----------


def test_the_screen_context_carries_the_code_dates(factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)
    run_committee(factory, demo_router, "ACME")
    with factory() as db:
        ctx = db.scalar(
            select(ResearchArtifact).where(
                ResearchArtifact.artifact_type == ArtifactType.screen_context
            )
        ).content
    assert ctx["technical"]["days_since_earnings"] == 25
    assert ctx["technical"]["days_to_earnings"] == 32
    assert (
        ctx["technical"]["next_earnings_date"] == (demo_data.TODAY + timedelta(days=32)).isoformat()
    )
