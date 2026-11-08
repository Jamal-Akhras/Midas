"""Provider abstraction. The ONLY place vendor SDKs/HTTP details may live is
the concrete provider modules. Product logic never sees a vendor type.

New vs the parent platform: tool calling. Providers accept a generic message
list and tool specs; they return either final text or tool_calls. The router
owns the loop, tool execution, snapshots, and cost logging.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema for arguments


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelResponse:
    provider: str
    model: str
    text: str  # final text ("" if tool_calls present)
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0


# Generic message shapes (provider adapters translate):
#   {"role": "user"|"assistant", "content": str}
#   {"role": "assistant", "tool_calls": [{"id","name","arguments"}], "content": str}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str}
Message = dict[str, Any]


class ModelProvider(Protocol):
    name: str

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
    ) -> ModelResponse: ...


class ProviderError(Exception):
    """Raised by providers on API failure; triggers router fallback."""
