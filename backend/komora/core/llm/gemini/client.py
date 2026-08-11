"""Gemini implementation of `LLMClient`.

Uses the **generateContent** API rather than the newer Interactions API. Interactions
has no explicit context caching, and it stores conversation history server-side by
default — a data-residency decision we will not drift into with users' receipt data.

Three settings here are deliberate and easy to get wrong:

* **Automatic function calling is disabled.** It would drive the tool loop for us, but
  it hides every step: no logging, no "typing…" updates, no chance to confirm before a
  side-effectful call.
* **`temperature` is never set.** Google warns that changing it on Gemini 3 risks
  looping or degraded output, and it is deprecated outright on 3.6. The usual
  `temperature=0` reflex for deterministic tool calling is counter-indicated.
* **`thinking_level` is explicit.** It defaults to `high`, which adds latency and bills
  thinking tokens at the output rate.

The system instruction and tool declarations are assembled identically on every call,
which is what lets implicit context caching hit.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from google import genai
from google.genai import types

from komora.core.llm.gemini.schema_map import json_schema_to_gemini
from komora.core.llm.protocol import LLMResponse, LLMUnavailable, Message, ToolCall, ToolDecl

_ROLE_TO_GEMINI = {"user": "user", "assistant": "model"}


class GeminiClient:
    """Bound to one model. Tier selection happens in config, not here."""

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        thinking_level: str = "low",
        sdk: Any | None = None,
    ) -> None:
        self.model = model
        self._thinking_level = thinking_level
        self._sdk = sdk if sdk is not None else genai.Client(api_key=api_key)

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDecl] = (),
    ) -> LLMResponse:
        config = self._build_config(system, tools)
        contents = [self._to_content(m) for m in messages]

        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                response = await self._sdk.aio.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except Exception as exc:
                last_error = exc
                if attempt == 2:
                    break
                await asyncio.sleep(0)
                continue
            return self._to_response(response)

        raise LLMUnavailable(f"Gemini ({self.model}) failed after a retry: {last_error}")

    def _build_config(self, system: str, tools: Sequence[ToolDecl]) -> types.GenerateContentConfig:
        declarations = [
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=types.Schema(**json_schema_to_gemini(tool.parameters)),
            )
            for tool in tools
        ]
        return types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            # We drive the loop ourselves — see the module docstring.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_level=self._thinking_level),
        )

    @staticmethod
    def _to_content(message: Message) -> types.Content:
        if message.role == "tool":
            # `Part.from_function_response` has no `id` parameter despite what
            # ai.google.dev shows, so the FunctionResponse is built directly —
            # without the id, parallel calls cannot be matched to their results.
            return types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=message.tool_call_id,
                            name=message.tool_name or "",
                            response={"result": message.content},
                        )
                    )
                ],
            )

        parts: list[types.Part] = []
        if message.content:
            parts.append(types.Part(text=message.content))
        parts.extend(
            types.Part(function_call=types.FunctionCall(id=call.id, name=call.name, args=call.args))
            for call in message.tool_calls
        )
        return types.Content(role=_ROLE_TO_GEMINI[message.role], parts=parts)

    @staticmethod
    def _to_response(response: Any) -> LLMResponse:
        calls = tuple(
            ToolCall(name=call.name or "", args=dict(call.args or {}), id=call.id)
            for call in (response.function_calls or [])
        )
        return LLMResponse(text=response.text, tool_calls=calls)
