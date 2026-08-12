"""The LLM surface the agent depends on.

One protocol, several providers. Tool parameters travel as **raw JSON Schema**: each
provider converts as its API demands (Gemini needs a lossy OpenAPI-subset rewrite,
Ollama takes them nearly as-is), so no caller has to know which is in use.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant", "tool"]


class LLMUnavailable(Exception):
    """The provider could not be reached, or failed after a retry."""


@dataclass(frozen=True)
class ToolDecl:
    name: str
    description: str
    parameters: dict[str, Any]
    """Raw JSON Schema, exactly as the MCP server published it."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    """Present on providers that supply one. Ollama's Python surface has none, so
    results are matched by name and order there."""
    provider_state: Any = None
    """Opaque provider data that must be echoed back on the following request.

    Gemini 3 attaches a `thought_signature` to the part carrying a function call and
    **rejects the next request without it** — `400 INVALID_ARGUMENT: Function call is
    missing a thought signature`. So a tool call cannot be reduced to (name, args, id)
    and rebuilt; something has to survive the round trip. Nothing outside the provider
    that produced it may read this.
    """


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    """Set on an assistant turn that requested tools."""
    tool_name: str | None = None
    """Which tool produced this result. Required when role is "tool"."""
    tool_call_id: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class LLMClient(Protocol):
    """Method-only so `isinstance` checks stay meaningful.

    A client is bound to one model at construction; tier selection happens in config
    (`Settings.tier_ref`), not here.
    """

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDecl] = (),
    ) -> LLMResponse: ...
