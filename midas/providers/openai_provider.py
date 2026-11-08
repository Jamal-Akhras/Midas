"""OpenAI provider (Chat Completions via httpx)."""

import json
import time

import httpx

from midas.config import settings
from midas.providers.base import Message, ModelResponse, ProviderError, ToolCall, ToolSpec

API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider:
    name = "openai"

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
            "messages": [{"role": "system", "content": system_prompt}, *_to_openai(messages)],
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        elif json_output:
            payload["response_format"] = {"type": "json_object"}
        start = time.monotonic()
        try:
            for attempt in (1, 2, 3):
                try:
                    resp = httpx.post(
                        API_URL,
                        json=payload,
                        timeout=120,
                        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    )
                    # transient gateway errors (Cloudflare 520/522, 500/502/
                    # 503/529): brief backoff and retry before giving up
                    if resp.status_code >= 500 and attempt < 3:
                        time.sleep(2 * attempt)
                        continue
                    resp.raise_for_status()
                    break
                except httpx.TransportError:
                    if attempt == 3:
                        raise
                    time.sleep(2 * attempt)
        except httpx.HTTPStatusError as exc:
            # Status + OpenAI's error message are safe to surface (the key
            # travels in a header, never in URLs or error bodies).
            detail = ""
            try:
                detail = exc.response.json().get("error", {}).get("message", "")[:200]
            except Exception:
                detail = exc.response.text[:120]
            raise ProviderError(f"openai: HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"openai: {type(exc).__name__}") from exc
        data = resp.json()
        msg = data["choices"][0]["message"]
        tool_calls = [
            ToolCall(
                id=c["id"],
                name=c["function"]["name"],
                arguments=_json_or_empty(c["function"]["arguments"]),
            )
            for c in msg.get("tool_calls") or []
        ]
        usage = data.get("usage", {})
        return ModelResponse(
            provider=self.name,
            model=model,
            text=msg.get("content") or "",
            tool_calls=tool_calls,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            latency_ms=int((time.monotonic() - start) * 1000),
        )


def _json_or_empty(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _to_openai(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": json.dumps(c["arguments"]),
                            },
                        }
                        for c in m["tool_calls"]
                    ],
                }
            )
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out
