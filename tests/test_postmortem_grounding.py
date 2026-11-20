"""2026-11-20: a lesson has to be grounded in the entry's own numbers.

WFC and INTU both closed with the lesson "use a wider stop" and both kept
falling - WFC's stop was 1.2 ATR from the fill and saved money; INTU's
problem was a fill 6.5% under the prior close, below its own zone. Those
sentences were then fed to every later committee. The post-mortem is now
handed `entry_facts` computed in code (the fill against the prior close
and the zone, the stop distance in ATRs), says what its lesson is about,
and code keeps a stop lesson out of the committees' feed unless the stop
really was inside one ATR. Post-mortems written before this carry no
grounding mark and are not fed."""

import pytest

from midas.config import settings
from midas.models import ArtifactType, ResearchArtifact, TradeOutcome, TradeThesis
from midas.models.domain import PipelineRun, RunKind
from midas.providers import demo_provider
from midas.scheduler import monitor
from tests.test_postmortem_record import _closed_trade, _postmortem_prompt


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)


def _with_atr(db, outcome: TradeOutcome, atr: float) -> None:
    thesis = db.get(TradeThesis, outcome.thesis_id)
    db.add(
        ResearchArtifact(
            run_id=thesis.run_id,
            ticker=thesis.ticker,
            agent_slug="screen-context",
            artifact_type=ArtifactType.screen_context,
            content={"technical": {"atr14": atr}},
        )
    )
    db.flush()


def _says(monkeypatch, about: str, lesson: str) -> None:
    monkeypatch.setitem(
        demo_provider.SCRIPTS,
        "postmortem",
        (
            [],
            lambda t: {
                "ticker": t,
                "what_worked": [],
                "what_failed": ["stopped out"],
                "lesson": lesson,
                "lesson_about": about,
            },
        ),
    )


def _postmortem(db) -> dict:
    from sqlalchemy import select

    return db.scalar(
        select(ResearchArtifact).where(ResearchArtifact.artifact_type == ArtifactType.post_mortem)
    ).content


# ---------- the facts ----------


def test_the_postmortem_is_handed_the_entry_facts(factory, demo_router):
    with factory() as db:
        outcome = _closed_trade(db)
        _with_atr(db, outcome, 8.0)
        oid = outcome.id
        db.commit()
    monitor.run_post_mortems(factory, demo_router, [oid])
    with factory() as db:
        prompt = _postmortem_prompt(db)
        facts = _postmortem(db)["entry_facts"]
    assert '"entry_facts"' in prompt and '"stop_distance_atr": 1.59' in prompt
    assert facts["fill"] == 172.116 and facts["stop"] == 159.43
    assert facts["fill_vs_zone"] == "above the zone"  # 172.116 against 168.89-172.09
    assert facts["stop_distance_atr"] == 1.59  # 12.686 on an ATR of 8
    assert "prior_close" in facts and "fill_vs_prior_close_pct" in facts


def test_the_grounding_rule():
    assert monitor.lesson_grounded("stop", {"stop_distance_atr": 0.19}) == (True, "")
    ok, why = monitor.lesson_grounded("stop", {"stop_distance_atr": 1.2})
    assert ok is False
    assert why == (
        "the stop was 1.2 ATR from the fill - not a tight stop; the record does not support "
        "a lesson about stop distance"
    )
    ok, why = monitor.lesson_grounded("stop", {})
    assert ok is False and "unknown" in why
    assert monitor.lesson_grounded("entry", {"stop_distance_atr": 1.2}) == (True, "")
    assert monitor.lesson_grounded("other", {}) == (True, "")


# ---------- the feed ----------


def test_a_stop_lesson_about_a_stop_that_was_not_tight_is_not_fed(
    factory, demo_router, monkeypatch
):
    _says(monkeypatch, "stop", "set stops at a safer distance")
    with factory() as db:
        outcome = _closed_trade(db)
        _with_atr(db, outcome, 8.0)  # 1.59 ATR: the stop did its job
        oid = outcome.id
        db.commit()
    assert monitor.run_post_mortems(factory, demo_router, [oid]) == []
    with factory() as db:
        content = _postmortem(db)
        assert content["lesson_grounded"] is False and "1.59 ATR" in content["lesson_note"]
        assert monitor.recent_lessons(db) == []


def test_a_stop_lesson_about_a_tight_stop_is_fed(factory, demo_router, monkeypatch):
    _says(monkeypatch, "stop", "a stop inside the day's range is noise")
    with factory() as db:
        outcome = _closed_trade(db)
        _with_atr(db, outcome, 40.0)  # 0.32 ATR
        oid = outcome.id
        db.commit()
    lessons = monitor.run_post_mortems(factory, demo_router, [oid])
    assert lessons == ["a stop inside the day's range is noise"]
    with factory() as db:
        assert monitor.recent_lessons(db) == lessons


def test_postmortems_written_before_the_grounding_are_not_fed(db):
    run = PipelineRun(kind=RunKind.post_mortem, ticker="WFC")
    db.add(run)
    db.flush()
    db.add(
        ResearchArtifact(
            run_id=run.id,
            ticker="WFC",
            agent_slug="postmortem",
            artifact_type=ArtifactType.post_mortem,
            content={"ticker": "WFC", "lesson": "use a more conservative stop placement"},
        )
    )
    db.flush()
    assert monitor.recent_lessons(db) == []
