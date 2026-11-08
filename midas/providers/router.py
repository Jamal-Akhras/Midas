"""Model router: task-type + risk + cost routing, write-ahead cost logging,
and the agent tool loop.

Quality rules enforced here:
- Write-ahead cost logging: every provider response is recorded as a
  CostEvent + ModelCall BEFORE the parsed result reaches the caller.
  Failed parses, tool-loop intermediate calls, and fallbacks are logged.
- router_decision explains every choice (candidates, rejections, attempts).
- Schema validation: one targeted repair attempt, then the next candidate.
- Tool loop: bounded iterations; every tool result is snapshotted to
  DataSnapshot by the executor before the model sees it.
"""

import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from midas.models import CostEvent, ModelCall
from midas.providers.base import (
    Message,
    ModelProvider,
    ModelResponse,
    ProviderError,
    ToolSpec,
)

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)

ROUTING_PATH = Path(__file__).parent / "routing.yaml"
MAX_TOOL_ITERATIONS = 8
GATEWAY_DRIFT_ALERT = 0.25  # fraction by which the gateway's charge may differ from our estimate

# ToolExecutor: (tool_name, arguments) -> JSON-serializable result.
# Raising ToolError produces an error message the model can react to.
ToolExecutor = Callable[[str, dict], Any]


class RoutingError(Exception):
    """No candidate could produce a valid result (or all were priced out)."""


class ModelRouter:
    def __init__(self, providers: dict[str, ModelProvider], routing_path: Path = ROUTING_PATH):
        self.providers = providers
        table = yaml.safe_load(routing_path.read_text())
        self.models: dict[str, dict] = table["models"]
        self.task_types: dict[str, list[str]] = table["task_types"]
        self.risk_rules: dict[str, str] = table.get("risk_rules", {})

    # ---------- pricing / candidates ----------

    def _price(self, model: str, tokens_in: int, tokens_out: int) -> float:
        m = self.models[model]
        return (tokens_in * m["price_in"] + tokens_out * m["price_out"]) / 1_000_000

    def _estimate(self, model: str, system_prompt: str, user_prompt: str, max_tokens: int) -> float:
        est_in = (len(system_prompt) + len(user_prompt)) // 4
        return self._price(model, est_in, max_tokens)

    def _candidates(self, task_type: str, risk_level: str) -> tuple[list[str], list[dict]]:
        names = self.task_types.get(task_type)
        if not names:
            raise RoutingError(f"No routing entry for task_type '{task_type}'")
        rejected: list[dict] = []
        required = self.risk_rules.get("high_requires_tier")
        if risk_level == "high" and required:
            kept = [n for n in names if self.models[n]["tier"] == required]
            rejected = [
                {"model": n, "reason": f"risk=high requires tier={required}"}
                for n in names
                if n not in kept
            ]
            names = kept
        if not names:
            raise RoutingError(f"No candidates for '{task_type}' at risk={risk_level}")
        return names, rejected

    # ---------- write-ahead logging ----------

    def _log(
        self,
        db: Session,
        *,
        resp: ModelResponse,
        task_type: str,
        run_id: uuid.UUID | None,
        agent_slug: str | None,
        system_prompt: str,
        user_prompt: str,
        messages: list[Message],
        schema_name: str | None,
        parse_ok: bool,
        decision: dict[str, Any],
    ) -> float:
        cost = self._price(resp.model, resp.tokens_in, resp.tokens_out)
        event = CostEvent(
            run_id=run_id,
            agent_slug=agent_slug or "",
            task_type=task_type,
            provider=resp.provider,
            model=resp.model,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            cost_usd=cost,
            gateway_cost_usd=resp.gateway_cost_usd,
            latency_ms=resp.latency_ms,
        )
        db.add(event)
        db.flush()
        if resp.gateway_cost_usd is not None and cost > 0:
            drift = abs(resp.gateway_cost_usd / cost - 1.0)
            if drift > GATEWAY_DRIFT_ALERT:
                # the two ledgers disagree: routing.yaml prices are stale, or the
                # gateway's catalog is. Either way the cost log is wrong somewhere.
                from midas.audit import record_event

                logger.warning(
                    "gateway cost %.4f vs estimate %.4f for %s (%.0f%% apart)",
                    resp.gateway_cost_usd,
                    cost,
                    resp.model,
                    drift * 100,
                )
                record_event(
                    db,
                    event_type="cost.divergence",
                    summary=(
                        f"{resp.model}: gateway charged ${resp.gateway_cost_usd:.4f}, "
                        f"routing.yaml estimated ${cost:.4f} ({drift * 100:.0f}% apart)"
                    ),
                    run_id=run_id,
                    payload={
                        "gateway": resp.gateway_cost_usd,
                        "estimate": cost,
                        "model": resp.model,
                    },
                )
        db.add(
            ModelCall(
                cost_event_id=event.id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                messages=_safe_messages(messages),
                response_text=resp.text
                or json.dumps(
                    [{"tool": c.name, "arguments": c.arguments} for c in resp.tool_calls]
                ),
                output_schema_name=schema_name,
                parse_ok=parse_ok,
                router_decision=decision,
            )
        )
        db.flush()
        return cost

    # ---------- main entry ----------

    def run(
        self,
        db: Session,
        *,
        task_type: str,
        output_schema: type[T],
        system_prompt: str,
        user_prompt: str,
        risk_level: str = "low",
        max_cost_usd: float = 1.0,
        max_tokens: int = 4000,
        run_id: uuid.UUID | None = None,
        agent_slug: str | None = None,
        tools: list[ToolSpec] | None = None,
        tool_executor: ToolExecutor | None = None,
    ) -> T:
        """Run a task to a schema-validated result, with optional tools.

        With tools: bounded agentic loop; every intermediate call is logged.
        Without: single-shot with one repair attempt, then next candidate.
        """
        candidates, rejected = self._candidates(task_type, risk_level)
        attempts: list[dict] = []
        cost_skips = 0
        schema = output_schema.model_json_schema()
        spent = 0.0

        for model_name in candidates:
            provider_name = self.models[model_name]["provider"]
            provider = self.providers.get(provider_name)
            if provider is None:
                attempts.append(
                    {"model": model_name, "outcome": f"provider '{provider_name}' not registered"}
                )
                continue
            est = self._estimate(model_name, system_prompt, user_prompt, max_tokens)
            if est > max_cost_usd - spent:
                attempts.append(
                    {"model": model_name, "outcome": f"skipped: est ${est:.3f} > remaining ceiling"}
                )
                cost_skips += 1
                continue

            decision_base = {
                "task_type": task_type,
                "risk_level": risk_level,
                "chosen": model_name,
                "rejected_by_risk": rejected,
                "prior_attempts": list(attempts),
                "estimated_cost_usd": round(est, 4),
                "tools_enabled": bool(tools),
            }
            try:
                parsed, spent = self._attempt_model(
                    db,
                    provider=provider,
                    model_name=model_name,
                    task_type=task_type,
                    output_schema=output_schema,
                    schema=schema,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_tokens=max_tokens,
                    run_id=run_id,
                    agent_slug=agent_slug,
                    tools=tools,
                    tool_executor=tool_executor,
                    decision_base=decision_base,
                    spent=spent,
                    max_cost_usd=max_cost_usd,
                )
            except ProviderError as exc:
                attempts.append({"model": model_name, "outcome": f"provider error: {exc}"})
                continue
            except _ParseFailed:
                attempts.append({"model": model_name, "outcome": "parse failed (first+repair)"})
                continue
            return parsed

        if cost_skips and cost_skips == len(candidates):
            raise RoutingError(
                f"Cost ceiling: every candidate for '{task_type}' was estimated above "
                f"max_cost_usd=${max_cost_usd:.2f}: {attempts}"
            )
        raise RoutingError(f"All candidates failed for '{task_type}': {attempts}")

    # ---------- per-model attempt (tool loop + parse/repair) ----------

    def _attempt_model(
        self,
        db: Session,
        *,
        provider: ModelProvider,
        model_name: str,
        task_type: str,
        output_schema: type[T],
        schema: dict,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        run_id: uuid.UUID | None,
        agent_slug: str | None,
        tools: list[ToolSpec] | None,
        tool_executor: ToolExecutor | None,
        decision_base: dict,
        spent: float,
        max_cost_usd: float,
    ) -> tuple[T, float]:
        messages: list[Message] = [{"role": "user", "content": user_prompt}]

        # -- tool loop (bounded) --
        if tools and tool_executor is not None:
            tool_chars = 0
            for iteration in range(MAX_TOOL_ITERATIONS):
                resp = provider.complete(
                    model=model_name,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools,
                    json_output=False,
                    max_tokens=max_tokens,
                )
                decision = {**decision_base, "phase": "tool_loop", "iteration": iteration}
                spent += self._log(
                    db,
                    resp=resp,
                    task_type=task_type,
                    run_id=run_id,
                    agent_slug=agent_slug,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    messages=messages,
                    schema_name=None,
                    parse_ok=True,
                    decision=decision,
                )
                if not resp.tool_calls:
                    # model is done researching; fall through to parse phase
                    messages.append({"role": "assistant", "content": resp.text})
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": resp.text,
                        "tool_calls": [
                            {"id": c.id, "name": c.name, "arguments": c.arguments}
                            for c in resp.tool_calls
                        ],
                    }
                )
                for call in resp.tool_calls:
                    result = _run_tool(tool_executor, call.name, call.arguments)
                    # Context budget: full data lives in the snapshot table;
                    # the CONVERSATION gets a bounded slice. 84 tool calls x
                    # 20k chars once blew straight past the model's context
                    # window and failed every screen (2026-07-13).
                    text = json.dumps(result, default=str)[:6000]
                    tool_chars += len(text)
                    if tool_chars > 90000:
                        text = (
                            text[:400] + '"}]... [TOOL-RESULT BUDGET '
                            "EXHAUSTED: stop calling tools and produce "
                            "your final answer from the data above]"
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": text,
                        }
                    )
                if spent >= max_cost_usd:
                    logger.warning("tool loop for %s stopped by cost ceiling", agent_slug)
                    break
            else:
                logger.warning("tool loop for %s hit MAX_TOOL_ITERATIONS", agent_slug)

        # -- final structured answer, one repair attempt --
        ask = (
            "Now produce your final answer as ONLY a valid JSON object matching "
            f"this schema (no prose outside the JSON):\n{json.dumps(schema)}"
        )
        messages.append({"role": "user", "content": ask})
        for attempt in ("first", "repair"):
            resp = provider.complete(
                model=model_name,
                system_prompt=system_prompt,
                messages=messages,
                tools=None,
                json_output=True,
                json_schema=schema,
                max_tokens=max_tokens,
            )
            decision = {**decision_base, "phase": "final", "attempt": attempt}
            try:
                parsed = output_schema.model_validate(_extract_json(resp.text))
                spent += self._log(
                    db,
                    resp=resp,
                    task_type=task_type,
                    run_id=run_id,
                    agent_slug=agent_slug,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    messages=messages,
                    schema_name=output_schema.__name__,
                    parse_ok=True,
                    decision=decision,
                )
                return parsed, spent
            except (ValidationError, ValueError) as exc:
                spent += self._log(
                    db,
                    resp=resp,
                    task_type=task_type,
                    run_id=run_id,
                    agent_slug=agent_slug,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    messages=messages,
                    schema_name=output_schema.__name__,
                    parse_ok=False,
                    decision=decision,
                )
                if attempt == "first":
                    messages.append({"role": "assistant", "content": resp.text})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous response failed validation.\n"
                                f"Validation error:\n{str(exc)[:2000]}\n\n"
                                "Fix the problems and return ONLY a valid JSON object "
                                "matching the required schema."
                            ),
                        }
                    )
        raise _ParseFailed()


class _ParseFailed(Exception):
    pass


def _run_tool(executor: ToolExecutor, name: str, arguments: dict) -> Any:
    """Tool failures become data the model can react to, never a crash."""
    try:
        return executor(name, arguments)
    except Exception as exc:  # noqa: BLE001 - honest-data rule: report, don't invent
        logger.warning("tool %s failed: %s", name, exc)
        return {"error": f"{type(exc).__name__}: {exc}", "tool": name}


def _safe_messages(messages: list[Message]) -> list[Message]:
    """Messages are stored verbatim in the audit trail; cap size defensively."""
    out = []
    for m in messages:
        m = dict(m)
        if isinstance(m.get("content"), str) and len(m["content"]) > 30000:
            m["content"] = m["content"][:30000] + " ...[truncated for storage]"
        out.append(m)
    return out


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in response")
    return json.loads(text[start : end + 1])


_default_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _default_router
    if _default_router is None:
        from midas.config import settings

        if settings.effective_demo_mode:
            from midas.providers.demo_provider import DemoProvider

            demo = DemoProvider()
            _default_router = ModelRouter(
                providers={"openai": demo, "anthropic": demo, "gemini": demo}
            )
        else:
            providers: dict[str, ModelProvider] = {}
            if settings.openai_api_key:
                from midas.providers.openai_provider import OpenAIProvider

                providers["openai"] = OpenAIProvider()
            if settings.anthropic_api_key:
                from midas.providers.anthropic_provider import AnthropicProvider

                providers["anthropic"] = AnthropicProvider()
            if settings.gemini_api_key:
                from midas.providers.gemini_provider import GeminiProvider

                providers["gemini"] = GeminiProvider()
            _default_router = ModelRouter(providers=providers)
    return _default_router
