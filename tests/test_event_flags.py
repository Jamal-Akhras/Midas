"""Event flags: schema hygiene, the demo scan path, outcome scoring."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from midas.agents.schemas import EventFlagItem, EventFlagsOutput
from midas.config import settings
from midas.models import EventFlag, PipelineRun, ResearchArtifact, RunKind, RunStatus, utcnow
from midas.pipeline.events import flag_stats, run_event_scan, update_flag_outcomes


@pytest.fixture(autouse=True)
def demo(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)


def _item(**kw):
    d = dict(
        kind="guidance_cut",
        severity="high",
        direction="negative",
        summary="Guidance cut 20%",
        source_url="https://example.com/8k",
    )
    d.update(kw)
    return d


def test_flag_item_normalises_and_rejects_unsourced_claims():
    f = EventFlagItem(**_item(kind="Management Change", severity="HIGH", direction="bearish"))
    assert (f.kind, f.severity, f.direction) == ("management_change", "high", "negative")
    assert EventFlagItem(**_item(kind="something odd")).kind == "other"
    with pytest.raises(ValidationError, match="source URL"):
        EventFlagItem(**_item(source_url=""))
    with pytest.raises(ValidationError, match="severity"):
        EventFlagItem(**_item(severity="critical"))
    with pytest.raises(ValidationError, match="direction"):
        EventFlagItem(**_item(direction="sideways"))


def test_empty_flag_list_is_valid():
    out = EventFlagsOutput(ticker="ACME")
    assert out.flags == [] and out.unverified == []


def test_demo_scan_records_sourced_flags_with_a_price(factory, demo_router):
    results = run_event_scan(factory, demo_router, ["ACME", "DUNE"])
    assert [r["ticker"] for r in results] == ["ACME", "DUNE"]
    with factory() as db:
        flags = db.scalars(select(EventFlag)).all()
        assert len(flags) == 1
        flag = flags[0]
        assert flag.ticker == "ACME" and flag.severity == "medium" and flag.direction == "negative"
        assert flag.source_url.startswith("https://")
        assert flag.price_at_flag == pytest.approx(184.20)
        assert flag.artifact_id is not None
        art = db.get(ResearchArtifact, flag.artifact_id)
        assert art is not None and art.agent_slug == "event-analyst"
        runs = db.scalars(select(PipelineRun).where(PipelineRun.kind == RunKind.event_scan)).all()
        assert len(runs) == 2 and all(r.status == RunStatus.done for r in runs)


def test_flag_outcomes_fill_forward_returns_after_the_window(db, monkeypatch):
    flag = EventFlag(
        ticker="ACME",
        kind="guidance_cut",
        severity="high",
        direction="negative",
        summary="x",
        source_url="https://example.com",
        price_at_flag=200.0,
    )
    fresh = EventFlag(
        ticker="DUNE",
        kind="product",
        severity="low",
        direction="positive",
        summary="y",
        source_url="https://example.com",
        price_at_flag=90.0,
    )
    db.add_all([flag, fresh])
    db.flush()
    flag.created_at = utcnow() - timedelta(days=15)
    db.flush()
    assert update_flag_outcomes(db) == 1  # only the aged flag is scored
    assert flag.fwd_10d_pct == pytest.approx((184.20 / 200.0 - 1) * 100, abs=0.01)
    assert flag.fwd_20d_pct is None and fresh.fwd_10d_pct is None
    s = flag_stats(db)
    assert s["n_flags"] == 2 and s["n_scored"] == 1
    assert s["avg_fwd_10d_negative_high_pct"] == pytest.approx(-7.9, abs=0.01)
    assert s["avg_fwd_10d_positive_pct"] is None
