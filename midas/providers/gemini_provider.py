"""Gemini provider (generateContent via httpx). API key travels in the
x-goog-api-key HEADER - never a query param (keys in URLs leak into logs)."""

import time
import uuid

import httpx

from midas.config import settings
from midas.providers.base import Message, ModelResponse, ProviderError, ToolCall, ToolSpec

BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider:
    name = "gemini"

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
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": _to_gemini(messages),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {"name": t.name, "description": t.description, "parameters": t.parameters}
                        for t in tools
                    ]
                }
            ]
        elif json_output:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            if json_schema:
                payload["generationConfig"]["responseSchema"] = _strip_schema(json_schema)
        start = time.monotonic()
        try:
            # Free-tier RPM limits 429 easily during tool loops. Respect
            # Google's requested retry delay (up to 2 retries, <=65s each)
            # before giving up - a paced run beats a failed run.
            for attempt in range(3):
                resp = httpx.post(
                    f"{BASE}/{model}:generateContent",
                    json=payload,
                    timeout=120,
                    headers={"x-goog-api-key": settings.gemini_api_key},
                )
                if resp.status_code == 429 and attempt < 2:
                    delay = _retry_delay_seconds(resp)
                    if delay <= 65:
                        time.sleep(delay)
                        continue
                break
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Status + Google's error message are safe to surface (the key
            # travels in a header, never in URLs or error bodies).
            detail = ""
            try:
                detail = exc.response.json().get("error", {}).get("message", "")[:200]
            except Exception:
                detail = exc.response.text[:120]
            raise ProviderError(f"gemini: HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"gemini: {type(exc).__name__}") from exc
        data = resp.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError) as exc:
            raise ProviderError("gemini: empty or blocked response") from exc
        text_parts, tool_calls = [], []
        for p in parts:
            if "text" in p:
                text_parts.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append(
                    ToolCall(
                        id=str(uuid.uuid4()),
                        name=fc["name"],
                        arguments=fc.get("args") or {},
                    )
                )
        usage = data.get("usageMetadata", {})
        return ModelResponse(
            provider=self.name,
            model=model,
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            tokens_in=usage.get("promptTokenCount", 0),
            tokens_out=usage.get("candidatesTokenCount", 0),
            latency_ms=int((time.monotonic() - start) * 1000),
        )


def _retry_delay_seconds(resp) -> float:
    # Parse RetryInfo from a 429 body (retryDelay like '21s'); default 15s.
    try:
        for detail in resp.json().get("error", {}).get("details", []):
            delay = str(detail.get("retryDelay", ""))
            if delay.endswith("s"):
                return min(float(delay[:-1]) + 1, 65.0)
    except Exception:
        pass
    return 15.0


def _strip_schema(schema: dict) -> dict:
    """Gemini responseSchema rejects some JSON-schema keys; drop them."""
    drop = {"$defs", "additionalProperties", "title", "default"}

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items() if k not in drop}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def _to_gemini(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "tool":
            out.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": m["name"],
                                "response": {"content": m["content"]},
                            }
                        }
                    ],
                }
            )
        elif m["role"] == "assistant" and m.get("tool_calls"):
            parts: list[dict] = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            for c in m["tool_calls"]:
                parts.append({"functionCall": {"name": c["name"], "args": c["arguments"]}})
            out.append({"role": "model", "parts": parts})
        else:
            role = "model" if m["role"] == "assistant" else "user"
            out.append({"role": role, "parts": [{"text": m["content"]}]})
    return out
