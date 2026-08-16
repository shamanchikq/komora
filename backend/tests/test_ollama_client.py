"""OllamaClient — raw HTTP against /api/chat.

Deliberately not the `ollama` Python package: its tool-parameter model keeps only
{type, items, description, enum} and silently drops nested `properties`, which would
destroy a basket schema. See docs/local-models-ollama-gemma.md §2.1.
"""

import json
from typing import Any

import httpx
import pytest
import respx

from komora.core.llm.ollama.client import OllamaClient
from komora.core.llm.protocol import LLMUnavailable, Message, ToolCall, ToolDecl

BASE = "http://localhost:11434"
CHAT = f"{BASE}/api/chat"

BASKET = ToolDecl(
    name="propose_basket",
    description="Запропонувати кошик",
    parameters={
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"description": {"type": "string"}},
                    "required": ["description"],
                },
            }
        },
        "required": ["lines"],
    },
)


def ok(message: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"model": "gemma4:12b", "message": message, "done": True})


def client() -> OllamaClient:
    return OllamaClient(model="gemma4:12b", base_url=BASE)


class TestRequestShape:
    @respx.mock
    async def test_posts_to_api_chat_without_streaming(self) -> None:
        route = respx.post(CHAT).mock(return_value=ok({"role": "assistant", "content": "привіт"}))
        await client().complete(system="s", messages=[Message("user", "привіт")])
        body = json.loads(route.calls[0].request.content)
        assert body["model"] == "gemma4:12b"
        assert body["stream"] is False

    @respx.mock
    async def test_system_prompt_leads_the_message_list(self) -> None:
        route = respx.post(CHAT).mock(return_value=ok({"role": "assistant", "content": "x"}))
        await client().complete(system="Ти — Комора.", messages=[Message("user", "hi")])
        messages = json.loads(route.calls[0].request.content)["messages"]
        assert messages[0] == {"role": "system", "content": "Ти — Комора."}

    @respx.mock
    async def test_num_ctx_is_set_explicitly(self) -> None:
        """Default context is 4096 below 23GiB VRAM, and Ollama SILENTLY drops the
        oldest messages rather than erroring."""
        route = respx.post(CHAT).mock(return_value=ok({"role": "assistant", "content": "x"}))
        await client().complete(system="s", messages=[Message("user", "hi")], tools=[BASKET])
        options = json.loads(route.calls[0].request.content)["options"]
        assert options["num_ctx"] >= 32768

    @respx.mock
    async def test_nested_tool_schema_is_sent_verbatim(self) -> None:
        """No Gemini rewrite here — that conversion is lossy and Ollama does not need it."""
        route = respx.post(CHAT).mock(return_value=ok({"role": "assistant", "content": "x"}))
        await client().complete(system="s", messages=[Message("user", "hi")], tools=[BASKET])
        tool = json.loads(route.calls[0].request.content)["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "propose_basket"
        nested = tool["function"]["parameters"]["properties"]["lines"]["items"]
        assert nested["properties"]["description"]["type"] == "string", "nesting must survive"

    @respx.mock
    async def test_tool_results_are_keyed_by_name(self) -> None:
        """Ollama matches results by tool_name; it has no tool_call_id on this surface."""
        route = respx.post(CHAT).mock(return_value=ok({"role": "assistant", "content": "x"}))
        await client().complete(
            system="s",
            messages=[
                Message("user", "hi"),
                Message("assistant", tool_calls=(ToolCall("t", {"a": 1}),)),
                Message("tool", '{"ok":true}', tool_name="t"),
            ],
        )
        last = json.loads(route.calls[0].request.content)["messages"][-1]
        assert last == {"role": "tool", "tool_name": "t", "content": '{"ok":true}'}


class TestResponses:
    @respx.mock
    async def test_plain_text(self) -> None:
        respx.post(CHAT).mock(return_value=ok({"role": "assistant", "content": "Ось кошик"}))
        result = await client().complete(system="s", messages=[Message("user", "hi")])
        assert result.text == "Ось кошик"
        assert result.wants_tools is False

    @respx.mock
    async def test_tool_call_with_dict_arguments(self) -> None:
        """Ollama returns a real JSON object here, unlike OpenAI's stringified form."""
        respx.post(CHAT).mock(
            return_value=ok(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "propose_basket", "arguments": {"lines": []}}}
                    ],
                }
            )
        )
        result = await client().complete(system="s", messages=[Message("user", "hi")])
        assert result.tool_calls[0].name == "propose_basket"
        assert result.tool_calls[0].args == {"lines": []}

    @respx.mock
    async def test_tool_call_with_stringified_arguments(self) -> None:
        """The OpenAI-compatible surface stringifies them — accept both."""
        respx.post(CHAT).mock(
            return_value=ok(
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "t", "arguments": '{"a": 1}'}}],
                }
            )
        )
        result = await client().complete(system="s", messages=[Message("user", "hi")])
        assert result.tool_calls[0].args == {"a": 1}

    @respx.mock
    async def test_unparseable_arguments_do_not_crash_the_loop(self) -> None:
        respx.post(CHAT).mock(
            return_value=ok(
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "t", "arguments": "not json"}}],
                }
            )
        )
        result = await client().complete(system="s", messages=[Message("user", "hi")])
        assert result.tool_calls[0].args == {}

    @respx.mock
    async def test_several_tool_calls_are_all_returned(self) -> None:
        respx.post(CHAT).mock(
            return_value=ok(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "a", "arguments": {}}},
                        {"function": {"name": "b", "arguments": {}}},
                    ],
                }
            )
        )
        result = await client().complete(system="s", messages=[Message("user", "hi")])
        assert [c.name for c in result.tool_calls] == ["a", "b"]


class TestFailures:
    @respx.mock
    async def test_transient_error_is_retried_once(self) -> None:
        route = respx.post(CHAT).mock(
            side_effect=[httpx.Response(503), ok({"role": "assistant", "content": "recovered"})]
        )
        result = await client().complete(system="s", messages=[Message("user", "hi")])
        assert result.text == "recovered"
        assert len(route.calls) == 2

    @respx.mock
    async def test_daemon_not_running_is_reported_clearly(self) -> None:
        respx.post(CHAT).mock(side_effect=httpx.ConnectError("connection refused"))
        with pytest.raises(LLMUnavailable, match="Ollama"):
            await client().complete(system="s", messages=[Message("user", "hi")])

    @respx.mock
    async def test_a_rejected_request_is_not_retried(self) -> None:
        """404 is Ollama saying it has no such model. Asking twice does not install it."""
        route = respx.post(CHAT).mock(return_value=httpx.Response(404))
        with pytest.raises(LLMUnavailable, match="refused"):
            await client().complete(system="s", messages=[Message("user", "hi")])
        assert len(route.calls) == 1
