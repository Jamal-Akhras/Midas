"""Optional LLM gateway: configurable base URLs, the gateway's cost header
captured as a second ledger, and a divergence alert when it disagrees with
the router's own estimate."""

import json

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from midas.config import settings
from midas.models import AuditEvent, CostEvent
from midas.providers import anthropic_provider, openai_provider
from midas.providers.base import ModelResponse, gateway_cost
from midas.providers.router import ModelRouter
from tests.conftest import FakeProvider


def test_gateway_cost_header_parsing():
    assert gateway_cost({"X-Tollgate-Cost": "0.0412 USD"}) == pytest.approx(0.0412)
    assert gateway_cost(httpx.Headers({"x-tollgate-cost": "1.5 USD"})) == pytest.approx(1.5)
    assert gateway_cost({}) is None
    assert gateway_cost({"X-Tollgate-Cost": "free"}) is None
    assert gateway_cost(None) is None


def _capture(monkeypatch, module, body: dict, headers: dict | None = None) -> dict:
    seen: dict = {}

    def fake_post(url, *, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        seen["url"] = url
        seen["payload"] = json
        return httpx.Response(
            200, json=body, headers=headers_out, request=httpx.Request("POST", url)
        )

    headers_out = headers or {}
    monkeypatch.setattr(module.httpx, "post", fake_post)
    return seen


def test_openai_provider_uses_the_configured_base_url_and_records_gateway_cost(monkeypatch):
    monkeypatch.setattr(settings, "openai_base_url", "http://127.0.0.1:8080/v1/")
    monkeypatch.setattr(settings, "openai_api_key", "tg_tenant_key")
    body = {
        "choices": [{"message": {"content": json.dumps({"answer": "yes"})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    seen = _capture(monkeypatch, openai_provider, body, {"X-Tollgate-Cost": "0.0007 USD"})
    resp = openai_provider.OpenAIProvider().complete(
        model="gpt-4o-mini", system_prompt="s", messages=[{"role": "user", "content": "u"}]
    )
    assert seen["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert resp.gateway_cost_usd == pytest.approx(0.0007)
    assert resp.tokens_in == 10 and resp.text == json.dumps({"answer": "yes"})


def test_anthropic_provider_uses_the_configured_base_url(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_base_url", "http://127.0.0.1:8080/anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "tg_tenant_key")
    body = {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }
    seen = _capture(monkeypatch, anthropic_provider, body)
    resp = anthropic_provider.AnthropicProvider().complete(
        model="claude-haiku-5-5", system_prompt="s", messages=[{"role": "user", "content": "u"}]
    )
    assert seen["url"] == "http://127.0.0.1:8080/anthropic/v1/messages"
    assert resp.gateway_cost_usd is None  # no gateway header: direct vendor call
    assert resp.text == "ok"


def test_default_urls_are_the_vendors():
    assert openai_provider._url() == "https://api.openai.com/v1/chat/completions"
    assert anthropic_provider._url() == "https://api.anthropic.com/v1/messages"


class Out(BaseModel):
    answer: str


class GatewayProvider(FakeProvider):
    """A scripted provider whose responses carry a gateway charge."""

    def __init__(self, name: str, script: list, charge: float):
        super().__init__(name, script)
        self.charge = charge

    def complete(self, **kw) -> ModelResponse:
        resp = super().complete(**kw)
        resp.gateway_cost_usd = self.charge
        return resp


def _router(charge: float) -> ModelRouter:
    good = json.dumps({"answer": "yes"})
    return ModelRouter(
        providers={
            "anthropic": GatewayProvider("anthropic", [good], charge),
            "openai": FakeProvider("openai", []),
            "gemini": FakeProvider("gemini", []),
        }
    )


def test_router_records_the_gateway_charge_next_to_its_estimate(db):
    r = _router(charge=0.00002)  # 100 in / ~3 out tokens on haiku-5-5 is ~$0.000011
    r.run(db, task_type="triage", output_schema=Out, system_prompt="s", user_prompt="u")
    event = db.scalars(select(CostEvent)).one()
    assert event.gateway_cost_usd == pytest.approx(0.00002)
    assert event.cost_usd > 0
    # within tolerance? the fake charge is ~2x the estimate, so an alert is expected
    alerts = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "cost.divergence")).all()
    assert len(alerts) == 1
    assert alerts[0].payload["model"] == "claude-haiku-5-5"


def test_no_alert_when_the_ledgers_agree(db):
    r = _router(charge=0.0)
    # charge 0 would be infinitely far; use the estimate itself instead
    provider = r.providers["anthropic"]
    estimate = r._price("claude-haiku-5-5", 100, len(json.dumps({"answer": "yes"})) // 4)
    provider.charge = estimate * 1.05  # type: ignore[attr-defined]
    r.run(db, task_type="triage", output_schema=Out, system_prompt="s", user_prompt="u")
    alerts = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "cost.divergence")).all()
    assert alerts == []


def test_direct_vendor_calls_leave_the_gateway_column_empty(db):
    good = json.dumps({"answer": "yes"})
    r = ModelRouter(
        providers={
            "anthropic": FakeProvider("anthropic", [good]),
            "openai": FakeProvider("openai", []),
            "gemini": FakeProvider("gemini", []),
        }
    )
    r.run(db, task_type="triage", output_schema=Out, system_prompt="s", user_prompt="u")
    assert db.scalars(select(CostEvent)).one().gateway_cost_usd is None
