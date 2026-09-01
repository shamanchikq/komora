"""Ollama implementation of `LLMClient` — free, offline, no API key.

Development only. On the axis that matters for Komora — multi-turn tool use — the best
local candidate scores far below hosted models, so this is not a production default.
See docs/local-models-ollama-gemma.md.

Two implementation choices carry the reasoning:

* **Raw HTTP, not the `ollama` package.** That package's tool-parameter model keeps
  only `{type, items, description, enum}` and silently discards nested `properties`,
  which would reduce `propose_basket` to "an object with no fields" — no error, no
  warning. The Go server itself preserves nesting, so posting the JSON directly works.
* **Tool schemas are sent verbatim.** The Gemini rewrite is lossy and Ollama does not
  need it.
"""

from collections.abc import Sequence
from typing import Any

import httpx

from komora.core.llm.protocol import LLMResponse, LLMUnavailable, Message, ToolCall, ToolDecl
from komora.core.llm.toolargs import as_args

DEFAULT_NUM_CTX = 32768
"""Ollama defaults to 4096 on machines with under 23 GiB of VRAM, and silently drops
the oldest messages when the prompt overflows instead of erroring. Tool schemas are
re-rendered on every request, so the budget goes quickly."""


class OllamaClient:
    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        *,
        num_ctx: int = DEFAULT_NUM_CTX,
        timeout: float = 600.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._num_ctx = num_ctx
        self._timeout = timeout

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDecl] = (),
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [self._to_message(m) for m in messages],
            "stream": False,
            # These models are thinking-capable; thinking adds latency for no gain here.
            "think": False,
            "options": {"num_ctx": self._num_ctx},
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]

        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for attempt in (1, 2):
                try:
                    response = await http.post(f"{self.base_url}/api/chat", json=payload)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    # A 4xx is about what we sent — an unknown model, a schema Ollama
                    # will not take — and asking again changes nothing. (The old
                    # `except (HTTPError, HTTPStatusError)` was one clause: the second
                    # is a subclass of the first, so it never caught anything of its
                    # own and every status retried alike.)
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status is not None and status < 500:
                        raise LLMUnavailable(
                            f"Ollama ({self.model}) refused the request: {exc}"
                        ) from exc
                    last_error = exc
                    if attempt == 2:
                        break
                    continue
                return self._to_response(response.json())

        raise LLMUnavailable(
            f"Ollama ({self.model}) at {self.base_url} failed after a retry: {last_error}. "
            f"Is the daemon running?"
        )

    @staticmethod
    def _to_message(message: Message) -> dict[str, Any]:
        if message.role == "tool":
            # Results are matched by tool_name — this surface has no tool_call_id.
            return {
                "role": "tool",
                "tool_name": message.tool_name or "",
                "content": message.content,
            }

        out: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            out["tool_calls"] = [
                {"function": {"name": call.name, "arguments": call.args}}
                for call in message.tool_calls
            ]
        return out

    @staticmethod
    def _to_response(body: dict[str, Any]) -> LLMResponse:
        message = body.get("message") or {}
        calls = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            calls.append(
                ToolCall(name=function.get("name", ""), args=as_args(function.get("arguments")))
            )
        return LLMResponse(text=message.get("content") or None, tool_calls=tuple(calls))
