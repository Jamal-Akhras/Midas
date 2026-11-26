"""2026-11-27: a scan gives the same answer to the same news, and reads it once.

The event scan re-read the same fortnight of headlines every day with a
model at its default temperature. One Netflix article produced a "high
negative guidance_cut" flag on seven consecutive scans (each one restarting
the 30-day veto and counting again in the scored ledger), its companion
story flipped between negative and neutral, and a third went from medium to
high overnight. Underneath, the news tool sorted Finnhub's epoch dates and
Alpaca's ISO dates as text, so Finnhub's headlines always ranked last and
were cut; and the three ETFs in the core book answered every filings call
with an error.

Now the scan remembers what it has shown the analyst and shows only what
is new (nothing new: no model call at all), a story already flagged is not
flagged again, the scan tasks run at temperature zero, and both news
sources are ordered by time."""

from datetime import datetime, timezone

import pytest
import yaml
from sqlalchemy import select

from midas.agents.schemas import EventFlagsOutput
from midas.config import settings
from midas.models import AuditEvent, CostEvent, EventFlag, ModelCall, utcnow
from midas.pipeline import events
from midas.providers import demo_provider, openai_provider
from midas.providers.router import ROUTING_PATH, ModelRouter
from midas.tools import demo_data, edgar
from midas.tools import fundamentals_news as news
from midas.tools.base import ToolError
from tests.conftest import FakeProvider
from tests.test_gateway import _capture


def _epoch(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


# ---------- the news feed: one clock for both sources ----------


@pytest.fixture()
def live_news(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "openai_api_key", "x")
    monkeypatch.setattr(settings, "finnhub_api_key", "x")
    monkeypatch.setattr(settings, "alpaca_api_key", "x")
    monkeypatch.setattr(settings, "alpaca_api_secret", "x")

    def feed(finnhub: list[dict], alpaca: list[dict]) -> None:
        monkeypatch.setattr(news, "_finnhub_news", lambda t, n: finnhub)
        monkeypatch.setattr(news, "_alpaca_news", lambda t, n: alpaca)

    return feed


def test_both_news_sources_are_ordered_by_time(live_news):
    live_news(
        [{"headline": "Newest, from Finnhub", "date": _epoch("2026-11-26T12:00:00"), "url": "f1"}],
        [
            {"headline": "Older, from Alpaca", "date": "2026-11-25T10:00:00Z", "url": "a1"},
            {"headline": "Oldest, from Alpaca", "date": "2026-11-20T10:00:00Z", "url": "a2"},
        ],
    )
    items = news.get_news("NFLX").data
    assert [n["url"] for n in items] == ["f1", "a1", "a2"]
    assert [n["date"] for n in items] == [
        "2026-11-26T12:00:00Z",
        "2026-11-25T10:00:00Z",
        "2026-11-20T10:00:00Z",
    ]


def test_the_cut_keeps_the_newest_whatever_their_source(live_news):
    old = [
        {"headline": f"Alpaca item {i}", "date": f"2026-11-{i + 1:02d}T10:00:00Z", "url": f"a{i}"}
        for i in range(12)
    ]
    fresh = [
        {"headline": f"Finnhub item {i}", "date": _epoch(f"2026-11-2{i}T09:00:00"), "url": f"f{i}"}
        for i in range(3)
    ]
    live_news(fresh, old)
    kept = [n["url"] for n in news.get_news("NFLX").data]
    assert len(kept) == 12 and kept[:3] == ["f2", "f1", "f0"]  # they used to be cut, all three


def test_an_undated_item_sorts_last_and_is_kept_undated(live_news):
    live_news(
        [{"headline": "No date", "date": "", "url": "f1"}],
        [{"headline": "Dated", "date": "2026-11-25T10:00:00Z", "url": "a1"}],
    )
    items = news.get_news("NFLX").data
    assert [n["url"] for n in items] == ["a1", "f1"] and items[1]["date"] == ""


# ---------- filings: a fund has none, which is an answer ----------


def test_a_ticker_with_no_sec_registrant_has_no_filings_not_an_error(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "openai_api_key", "x")

    def no_cik(ticker: str) -> str:
        raise ToolError(f"no CIK found for ticker {ticker}")

    monkeypatch.setattr(edgar, "_cik_for", no_cik)
    result = edgar.get_filings("QQQ")
    assert result.data == [] and result.source == "edgar"


# ---------- the scan: read once ----------


@pytest.fixture()
def demo(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)


def _flags(db) -> list[EventFlag]:
    return list(db.scalars(select(EventFlag).order_by(EventFlag.created_at)))


def _analyst_calls(db) -> int:
    return len(list(db.scalars(select(CostEvent).where(CostEvent.agent_slug == "event-analyst"))))


def _last_prompt(db) -> str:
    row = db.execute(
        select(ModelCall.user_prompt)
        .join(CostEvent, ModelCall.cost_event_id == CostEvent.id)
        .where(CostEvent.agent_slug == "event-analyst")
        .order_by(ModelCall.created_at.desc())
    ).first()
    assert row is not None
    return row[0]


def test_a_second_scan_of_the_same_news_does_not_call_the_model(demo, factory, demo_router):
    first = events.run_event_scan(factory, demo_router, ["ACME"])
    with factory() as db:
        calls, flags = _analyst_calls(db), len(_flags(db))
    assert flags == 1 and calls > 0 and "skipped" not in first[0]
    second = events.run_event_scan(factory, demo_router, ["ACME"])
    assert second == [{"ticker": "ACME", "flags": [], "skipped": "no new items"}]
    with factory() as db:
        assert _analyst_calls(db) == calls  # not one more token
        assert len(_flags(db)) == 1  # and the story is not flagged twice
        skipped = db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "flags.skipped")
        ).all()
        assert [e.summary for e in skipped] == ["ACME: nothing new since the last scan"]


def test_a_new_headline_reaches_the_analyst_and_only_the_new_one(
    demo, factory, demo_router, monkeypatch
):
    events.run_event_scan(factory, demo_router, ["DUNE"])
    with factory() as db:
        first_prompt = _last_prompt(db)
    assert "cloud-partnership" in first_prompt and "sector-rotation" in first_prompt
    fresh = {
        "headline": "Regulator opens inquiry into billing practices",
        "date": utcnow().date().isoformat(),
        "source": "demo-wire",
        "url": "https://news.example.com/demo/inquiry",
    }
    monkeypatch.setitem(demo_data.NEWS, "default", [fresh, *demo_data.NEWS["default"]])
    result = events.run_event_scan(factory, demo_router, ["DUNE"])
    assert "skipped" not in result[0]
    with factory() as db:
        prompt = _last_prompt(db)
    assert "https://news.example.com/demo/inquiry" in prompt
    assert "cloud-partnership" not in prompt and "sector-rotation" not in prompt
    assert "Judge only the items listed" in prompt


def _says(monkeypatch, flags: list[dict]) -> None:
    script = demo_provider.SCRIPTS["event-analyst"][0]
    monkeypatch.setitem(
        demo_provider.SCRIPTS,
        "event-analyst",
        (script, lambda t: {"ticker": t, "flags": flags, "unverified": []}),
    )


def _flag(**kw) -> dict:
    base = {
        "kind": "guidance_cut",
        "severity": "high",
        "direction": "negative",
        "summary": "Advertising revenue target lowered to $2.7bn",
        "source_url": "https://example.com/first-report",
        "event_date": "2026-11-18",
    }
    return {**base, **kw}


def _with_news(monkeypatch, url: str) -> None:
    item = {"headline": f"Story at {url}", "date": "2026-11-19", "source": "wire", "url": url}
    monkeypatch.setitem(demo_data.NEWS, "default", [item, *demo_data.NEWS["default"]])


def test_the_same_event_from_another_outlet_is_one_flag(demo, factory, demo_router, monkeypatch):
    _says(monkeypatch, [_flag()])
    events.run_event_scan(factory, demo_router, ["DUNE"])
    # the next day another outlet carries the story, stamped with a time and
    # judged a shade differently; it is the same event
    _with_news(monkeypatch, "https://example.com/second-report")
    _says(
        monkeypatch,
        [
            _flag(
                source_url="https://example.com/second-report",
                event_date="2026-11-18T22:31:40Z",
                severity="medium",
            )
        ],
    )
    result = events.run_event_scan(factory, demo_router, ["DUNE"])
    assert result[0]["flags"] == [] and result[0]["duplicates"] == 1
    with factory() as db:
        (flag,) = _flags(db)
        assert flag.severity == "high" and flag.source_url == "https://example.com/first-report"
        (dup,) = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "flags.duplicate"))
        assert dup.summary.startswith("DUNE: guidance_cut already flagged")


def test_a_different_event_is_still_a_new_flag(demo, factory, demo_router, monkeypatch):
    _says(monkeypatch, [_flag()])
    events.run_event_scan(factory, demo_router, ["DUNE"])
    _with_news(monkeypatch, "https://example.com/cfo")
    _says(
        monkeypatch,
        [
            _flag(  # another kind of event the same week
                kind="management_change",
                source_url="https://example.com/cfo",
                summary="CFO resigns",
            ),
            _flag(  # the same kind, three weeks later: a second cut is news
                source_url="https://example.com/second-cut",
                event_date="2026-12-10",
            ),
        ],
    )
    result = events.run_event_scan(factory, demo_router, ["DUNE"])
    assert len(result[0]["flags"]) == 2
    with factory() as db:
        assert len(_flags(db)) == 3


def test_the_duplicate_rule():
    old = EventFlag(
        ticker="NFLX",
        kind="guidance_cut",
        severity="high",
        direction="negative",
        summary="x",
        source_url="https://a.example/1",
        event_date="2026-11-18",
    )
    item = EventFlagsOutput(ticker="NFLX", flags=[_flag(source_url="https://b.example/2")]).flags[0]
    assert events.same_event(item, old) is True  # same kind, same day, another outlet
    undated = item.model_copy(update={"event_date": "", "source_url": "https://a.example/1"})
    assert events.same_event(undated, old) is True  # the same article
    elsewhere = item.model_copy(update={"event_date": ""})
    assert events.same_event(elsewhere, old) is False  # nothing ties them together
    later = item.model_copy(update={"event_date": "2026-11-25"})
    assert events.same_event(later, old) is False  # a week on: a new event
    other = item.model_copy(update={"kind": "litigation"})
    assert events.same_event(other, old) is False


def test_a_failed_scan_leaves_the_items_unread(demo, factory, demo_router, monkeypatch):
    def broken(ticker: str) -> dict:
        raise RuntimeError("model unavailable")

    script = demo_provider.SCRIPTS["event-analyst"][0]
    monkeypatch.setitem(demo_provider.SCRIPTS, "event-analyst", (script, broken))
    result = events.run_event_scan(factory, demo_router, ["DUNE"])
    assert "error" in result[0]
    monkeypatch.undo()
    monkeypatch.setattr(settings, "demo_mode", True)
    result = events.run_event_scan(factory, demo_router, ["DUNE"])
    assert "skipped" not in result[0] and "error" not in result[0]  # read on the next scan


# ---------- temperature: the same answer to the same question ----------


def test_the_scan_tasks_are_routed_at_temperature_zero():
    table = yaml.safe_load(ROUTING_PATH.read_text())
    assert table["task_options"]["event_scan"] == {"temperature": 0}
    assert table["task_options"]["screening"] == {"temperature": 0}
    assert set(table["task_options"]) <= set(table["task_types"])  # no option for an unknown task


class _Recording(FakeProvider):
    def complete(self, **kw):
        self.kwargs = getattr(self, "kwargs", []) + [kw]
        return super().complete(**{k: v for k, v in kw.items() if k != "temperature"})


def test_the_router_passes_a_tasks_temperature_and_only_where_one_is_set(db, tmp_path):
    table = yaml.safe_load(ROUTING_PATH.read_text())
    table["task_options"] = {"event_scan": {"temperature": 0}}
    path = tmp_path / "routing.yaml"
    path.write_text(yaml.safe_dump(table))
    answer = '{"ticker": "ACME", "flags": [], "unverified": []}'
    provider = _Recording("anthropic", [answer, answer])
    router = ModelRouter(providers={"anthropic": provider}, routing_path=path)
    for task in ("event_scan", "triage"):
        router.run(
            db,
            task_type=task,
            output_schema=EventFlagsOutput,
            system_prompt="s",
            user_prompt="u",
        )
    assert provider.kwargs[0]["temperature"] == 0
    assert "temperature" not in provider.kwargs[1]  # an unlisted task keeps the provider default


def test_the_openai_request_carries_the_temperature_when_given(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "k")
    body = {"choices": [{"message": {"content": "{}"}}], "usage": {}}
    seen = _capture(monkeypatch, openai_provider, body)
    call = dict(model="gpt-4o-mini", system_prompt="s", messages=[{"role": "user", "content": "u"}])
    openai_provider.OpenAIProvider().complete(**call, temperature=0)
    assert seen["payload"]["temperature"] == 0
    openai_provider.OpenAIProvider().complete(**call)
    assert "temperature" not in seen["payload"]
