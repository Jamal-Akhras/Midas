import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from midas.models import Base
from midas.providers.base import Message, ModelResponse, ProviderError, ToolSpec
from midas.providers.demo_provider import DemoProvider
from midas.providers.router import ModelRouter


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def db(factory):
    with factory() as session:
        yield session


@pytest.fixture()
def demo_router():
    demo = DemoProvider()
    return ModelRouter(providers={"openai": demo, "anthropic": demo, "gemini": demo})


class FakeProvider:
    """Scripted provider: pops responses in order. An entry may be a string
    (final text), a list of (name, args) tool calls, or provider_error."""

    def __init__(self, name: str, script: list):
        self.name = name
        self.script = list(script)
        self.calls: list[dict] = []

    def complete(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_output: bool = False,
        json_schema: dict | None = None,
        max_tokens: int = 4000,
    ) -> ModelResponse:
        self.calls.append(
            {"model": model, "messages": list(messages), "tools": tools, "json_output": json_output}
        )
        if not self.script:
            raise ProviderError(f"{self.name}: script exhausted")
        step = self.script.pop(0)
        if step is provider_error:
            raise ProviderError(f"{self.name}: scripted failure")
        if isinstance(step, list):
            from midas.providers.base import ToolCall

            return ModelResponse(
                provider=self.name,
                model=model,
                text="",
                tool_calls=[
                    ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(step)
                ],
                tokens_in=100,
                tokens_out=10,
                latency_ms=1,
            )
        return ModelResponse(
            provider=self.name,
            model=model,
            text=step,
            tokens_in=100,
            tokens_out=len(step) // 4,
            latency_ms=1,
        )


provider_error = object()


@pytest.fixture(autouse=True)
def _pin_market_state(monkeypatch):
    """Tests must not depend on the wall clock or network: pretend the
    market is open unless a test explicitly overrides."""
    from midas.scheduler import monitor

    real = {
        "session": monitor.market_session_open,
        "day": monitor._market_open_today,
        "hours": monitor._us_market_hours,
    }
    monkeypatch.setattr(monitor, "_real_market_fns", real, raising=False)
    import midas.config as _config

    monkeypatch.setattr(_config, "reload_settings", lambda: None)
    monkeypatch.setattr(_config.settings, "alpaca_mirror_orders", False)
    # the suite's baseline must not depend on the operator's live .env:
    # these are the values the tests were written against
    monkeypatch.setattr(_config.settings, "max_total_premium_pct", 10.0)
    monkeypatch.setattr(_config.settings, "paper_starting_cash", 100_000.0)
    monkeypatch.setattr(_config.settings, "daily_spend_limit_usd", 10.0)
    monkeypatch.setattr(_config.settings, "auto_cycle_max_committees", 4)
    monkeypatch.setattr(_config.settings, "universe", "demo")
    monkeypatch.setattr(_config.settings, "watchlist", "")
    monkeypatch.setattr(_config.settings, "deep_scan_enabled", False)
    monkeypatch.setattr(monitor, "market_session_open", lambda: True)
    monkeypatch.setattr(
        monitor, "market_regime", lambda db: {"risk_on": True, "detail": "test", "date": ""}
    )
    monkeypatch.setattr(monitor, "_market_open_today", lambda: True)
    monkeypatch.setattr(monitor, "_us_market_hours", lambda: True)
