"""Anthropic provider (Messages API via httpx). Key goes in headers, never
query params or error text."""

import time
import uuid

import httpx

from midas.config import settings
from midas.providers.base import Message, ModelResponse, ProviderError, ToolCall, ToolSpec

API_URL = "https://api.anthropic.com/v1/messages"


class AnthropicProvider:
    name = "anthropic"

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
        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": _to_anthropic(messages),
        }
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        start = time.monotonic()
        try:
            resp = httpx.post(
                API_URL,
                json=payload,
                timeout=120,
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                detail = exc.response.json().get("error", {}).get("message", "")[:200]
            except Exception:
                detail = exc.response.text[:120]
            raise ProviderError(f"anthropic: HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic: {type(exc).__name__}") from exc
        data = resp.json()
        text_parts, tool_calls = [], []
        for block in data.get("content", []):
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id") or str(uuid.uuid4()),
                        name=block["name"],
                        arguments=block.get("input") or {},
                    )
                )
        usage = data.get("usage", {})
        return ModelResponse(
            provider=self.name,
            model=model,
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            latency_ms=int((time.monotonic() - start) * 1000),
        )


def _to_anthropic(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m["tool_call_id"],
                "content": m["content"],
            }
            # consecutive tool results merge into one user turn
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            content: list[dict] = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for c in m["tool_calls"]:
                content.append(
                    {
                        "type": "tool_use",
                        "id": c["id"],
                        "name": c["name"],
                        "input": c["arguments"],
                    }
                )
            out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out
