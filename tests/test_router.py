"""Router: write-ahead logging, fallback, repair, cost ceiling, and the
bull/bear model-divergence rule."""

import json

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from midas.models import CostEvent, ModelCall
from midas.providers.router import ModelRouter, RoutingError
from tests.conftest import FakeProvider, provider_error


class Out(BaseModel):
    answer: str


GOOD = json.dumps({"answer": "yes"})
BAD = json.dumps({"nope": 1})


def router_with(openai_script=(), anthropic_script=(), gemini_script=()):
    return ModelRouter(
        providers={
            "openai": FakeProvider("openai", list(openai_script)),
            "anthropic": FakeProvider("anthropic", list(anthropic_script)),
            "gemini": FakeProvider("gemini", list(gemini_script)),
        }
    )


def test_success_logs_cost_and_call(db):
    r = router_with(openai_script=[GOOD])
    out = r.run(db, task_type="triage", output_schema=Out, system_prompt="s", user_prompt="u")
    assert out.answer == "yes"
    events = db.scalars(select(CostEvent)).all()
    calls = db.scalars(select(ModelCall)).all()
    assert len(events) == 1 and len(calls) == 1
    assert events[0].cost_usd > 0
    assert calls[0].parse_ok is True
    assert calls[0].router_decision["chosen"] == "gpt-4o-mini"


def test_failed_parse_is_logged_then_repaired(db):
    r = router_with(openai_script=[BAD, GOOD])
    out = r.run(db, task_type="triage", output_schema=Out, system_prompt="s", user_prompt="u")
    assert out.answer == "yes"
    calls = db.scalars(select(ModelCall)).all()
    assert [c.parse_ok for c in calls] == [False, True]
    # repair prompt must include the validation failure
    assert "failed validation" in calls[1].messages[-1]["content"]


def test_provider_error_falls_through(db):
    r = router_with(openai_script=[provider_error], gemini_script=[GOOD])
    out = r.run(db, task_type="triage", output_schema=Out, system_prompt="s", user_prompt="u")
    assert out.answer == "yes"


def test_all_fail_raises_with_attempts(db):
    r = router_with(
        openai_script=[provider_error],
        gemini_script=[provider_error],
        anthropic_script=[provider_error],
    )
    with pytest.raises(RoutingError, match="All candidates failed"):
        r.run(db, task_type="triage", output_schema=Out, system_prompt="s", user_prompt="u")


def test_cost_ceiling_skips_everything(db):
    r = router_with(openai_script=[GOOD])
    with pytest.raises(RoutingError, match="Cost ceiling"):
        r.run(
            db,
            task_type="triage",
            output_schema=Out,
            system_prompt="s" * 100_000,
            user_prompt="u",
            max_cost_usd=0.000001,
        )


def test_high_risk_requires_high_tier(db):
    r = router_with(anthropic_script=[GOOD])
    out = r.run(
        db,
        task_type="verdict",
        output_schema=Out,
        system_prompt="s",
        user_prompt="u",
        risk_level="high",
    )
    assert out.answer == "yes"
    call = db.scalars(select(ModelCall)).one()
    assert call.router_decision["chosen"] == "claude-sonnet-4-5"
    assert call.router_decision["rejected_by_risk"] == []  # verdict list is all-high already


def test_bull_and_bear_lead_with_different_providers():
    r = router_with()
    bull_first = r.task_types["bull_case"][0]
    bear_first = r.task_types["bear_case"][0]
    assert bull_first != bear_first
    assert r.models[bull_first]["provider"] != r.models[bear_first]["provider"]
