"""OpenRouter implementation of `LLMClient` — one key, many models.

Why it exists at all: **Gemini's free tier limits requests, not tokens**, per
(project, model) per day, and a basket spends one request on the proposal and one on
the verification. Komora already splits those across two Gemini models to draw on two
allowances (`config.Settings`). OpenRouter adds allowances that are not Google's, so a
tier can be moved off the quota that is actually biting without touching the pipeline.

Raw HTTP rather than the `openai` package, for the same reason `ollama/client.py` does
it: the dependency buys nothing here and its schema handling is its own risk. The wire
format is OpenAI's chat-completions, which is what OpenRouter speaks.

Two details that differ from the other providers:

* **Tool arguments arrive as a JSON string**, not an object — see `llm/toolargs.py`.
* **A tool result is matched by `tool_call_id`**, not by name and order. The id comes
  back on the assistant turn and must be echoed exactly; `Message.tool_call_id` already
  carries it, and `agent/loop.py` already fills it in for providers that supply one.

**Stealth models** (`stealth/*`) are previews from an undisclosed provider that may
change or vanish without notice, and OpenRouter's own terms say prompts and completions
are **retained by that provider**. Fine for evaluation; a deliberate decision for
anything carrying a real person's shopping.
"""

import json
from collections.abc import Sequence
from typing import Any

import httpx

from komora.core.llm.protocol import LLMResponse, LLMUnavailable, Message, ToolCall, ToolDecl
from komora.core.llm.toolargs import as_args

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

ATTRIBUTION = {
    # OpenRouter uses these for its public model-usage rankings. Optional, and sent
    # because a free tier that cannot see who is using it is a free tier that goes away.
    "HTTP-Referer": "https://github.com/shamanchikq/komora",
    "X-Title": "Komora",
}


class OpenRouterClient:
    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 300.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
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
        }
        if tools:
            # Schemas go verbatim. Only Gemini needs the lossy OpenAPI-subset rewrite.
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

        headers = {"Authorization": f"Bearer {self._api_key}", **ATTRIBUTION}
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as http:
            for attempt in (1, 2):
                try:
                    response = await http.post(f"{self.base_url}/chat/completions", json=payload)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    # 429 is the one 4xx worth retrying: on a free model it means the
                    # shared pool is busy this second, not that the request is wrong.
                    if status is not None and status < 500 and status != 429:
                        raise LLMUnavailable(
                            f"OpenRouter ({self.model}) refused the request: {_reason(exc) or exc}"
                        ) from exc
                    last_error = exc
                    if attempt == 2:
                        break
                    continue
                return self._to_response(response.json(), self.model)

        raise LLMUnavailable(
            f"OpenRouter ({self.model}) failed after a retry: {_reason(last_error) or last_error}"
        )

    @staticmethod
    def _to_message(message: Message) -> dict[str, Any]:
        if message.role == "tool":
            # OpenAI's shape pairs a result to its call by id. `tool_name` is kept for
            # providers that have no id; sending it here would be an unknown field.
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id or message.tool_name or "",
                "content": message.content,
            }

        out: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            out["tool_calls"] = [
                {
                    "id": call.id or f"call_{index}",
                    "type": "function",
                    "function": {"name": call.name, "arguments": _dumps(call.args)},
                }
                for index, call in enumerate(message.tool_calls)
            ]
        return out

    @staticmethod
    def _to_response(body: dict[str, Any], model: str) -> LLMResponse:
        # OpenRouter reports upstream failures as a 200 carrying `error`, so a body
        # with no `choices` is not an empty answer — it is an answer that never came.
        if body.get("error") is not None and not body.get("choices"):
            error = body["error"]
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise LLMUnavailable(f"OpenRouter ({model}) returned an error: {detail}")

        choices = body.get("choices") or []
        if not choices:
            raise LLMUnavailable(f"OpenRouter ({model}) returned no choices")

        message = choices[0].get("message") or {}
        calls = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            calls.append(
                ToolCall(
                    name=function.get("name", ""),
                    args=as_args(function.get("arguments")),
                    id=raw.get("id"),
                )
            )
        return LLMResponse(text=message.get("content") or None, tool_calls=tuple(calls))


def _dumps(args: dict[str, Any]) -> str:
    """Cyrillic stays Cyrillic — the model reads these back as the query it asked for."""
    return json.dumps(args, ensure_ascii=False)


def _reason(exc: Exception | None) -> str | None:
    """OpenRouter puts the useful sentence in the body, not the status line."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        error = response.json().get("error")
    except Exception:
        return None
    if isinstance(error, dict):
        message = error.get("message")
        return str(message) if message else None
    return str(error) if error else None
