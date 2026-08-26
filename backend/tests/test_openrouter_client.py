"""The OpenRouter client against a mocked wire.

What is worth asserting is the shape, not that httpx works: this provider differs from
the other two in three specific ways, and each was a real trap — arguments arrive as a
string, a result is paired by `tool_call_id` rather than by name, and a failed upstream
call comes back as **200 with an `error` body**, which reads as an empty answer to
anything that only checks the status.
"""

import json

import httpx
import pytest
import respx

from komora.core.llm.openrouter.client import DEFAULT_BASE_URL, OpenRouterClient
from komora.core.llm.protocol import LLMUnavailable, Message, ToolCall, ToolDecl

URL = f"{DEFAULT_BASE_URL}/chat/completions"
MODEL = "stealth/ox-alpha"

TOOL = ToolDecl(
    name="propose_basket",
    description="Propose a basket",
    parameters={
        "type": "object",
        "properties": {"lines": {"type": "array", "items": {"type": "object"}}},
        "required": ["lines"],
    },
)


def reply(message: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": message}]})


def client() -> OpenRouterClient:
    return OpenRouterClient(model=MODEL, api_key="or-test-key")


@respx.mock
async def test_a_plain_answer_comes_back_as_text() -> None:
    respx.post(URL).mock(return_value=reply({"content": "Молоко коштує 42,90 ₴"}))
    result = await client().complete(system="s", messages=[Message("user", "почім молоко")])
    assert result.text == "Молоко коштує 42,90 ₴"
    assert result.wants_tools is False


@respx.mock
async def test_tool_arguments_arrive_as_a_string_and_are_parsed() -> None:
    """The one difference that would silently empty every basket the model proposes."""
    respx.post(URL).mock(
        return_value=reply(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "propose_basket",
                            # A JSON *string*, which is what OpenAI's format specifies.
                            "arguments": json.dumps(
                                {"lines": [{"description": "молоко"}]}, ensure_ascii=False
                            ),
                        },
                    }
                ],
            }
        )
    )
    result = await client().complete(system="s", messages=[Message("user", "молоко")], tools=[TOOL])
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.name == "propose_basket"
    assert call.args == {"lines": [{"description": "молоко"}]}
    assert call.id == "call_abc", "the id must survive — the result is paired back by it"


@respx.mock
async def test_malformed_arguments_are_an_empty_call_not_a_crash() -> None:
    respx.post(URL).mock(
        return_value=reply(
            {"tool_calls": [{"id": "x", "function": {"name": "f", "arguments": "{oops"}}]}
        )
    )
    result = await client().complete(system="s", messages=[Message("user", "hi")], tools=[TOOL])
    assert result.tool_calls[0].args == {}


@respx.mock
async def test_the_request_carries_the_key_schemas_and_attribution() -> None:
    route = respx.post(URL).mock(return_value=reply({"content": "ok"}))
    await client().complete(system="ти Комора", messages=[Message("user", "молоко")], tools=[TOOL])

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer or-test-key"
    assert request.headers["x-title"] == "Komora"
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["messages"][0] == {"role": "system", "content": "ти Комора"}
    # Verbatim: only Gemini needs the lossy rewrite, and a flattened schema would turn
    # propose_basket into "an object with no fields" with no error anywhere.
    assert body["tools"][0]["function"]["parameters"] == TOOL.parameters


@respx.mock
async def test_a_tool_result_is_paired_by_id() -> None:
    route = respx.post(URL).mock(return_value=reply({"content": "ok"}))
    await client().complete(
        system="s",
        messages=[
            Message("user", "молоко"),
            Message("assistant", tool_calls=(ToolCall("search", {"q": "молоко"}, id="call_1"),)),
            Message("tool", "знайшов 30", tool_name="search", tool_call_id="call_1"),
        ],
    )
    sent = json.loads(route.calls.last.request.content)["messages"]
    assert sent[2]["tool_calls"][0]["id"] == "call_1"
    assert json.loads(sent[2]["tool_calls"][0]["function"]["arguments"]) == {"q": "молоко"}
    assert sent[3] == {"role": "tool", "tool_call_id": "call_1", "content": "знайшов 30"}
    assert "tool_name" not in sent[3], "an unknown field on this API, not a harmless extra"


@respx.mock
async def test_cyrillic_survives_the_arguments_round_trip() -> None:
    route = respx.post(URL).mock(return_value=reply({"content": "ok"}))
    await client().complete(
        system="s",
        messages=[Message("assistant", tool_calls=(ToolCall("search", {"q": "ковбаса"}),))],
    )
    sent = json.loads(route.calls.last.request.content)["messages"][1]
    assert "ковбаса" in sent["tool_calls"][0]["function"]["arguments"]


@respx.mock
async def test_a_200_carrying_an_error_is_not_an_empty_answer() -> None:
    """OpenRouter reports an upstream failure inside a 200. Read only the status and a
    dead provider looks like a model that chose to say nothing."""
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"error": {"message": "upstream is down"}})
    )
    with pytest.raises(LLMUnavailable, match="upstream is down"):
        await client().complete(system="s", messages=[Message("user", "hi")])


@respx.mock
async def test_a_bad_request_is_not_retried() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "no such model"}})
    )
    with pytest.raises(LLMUnavailable, match="no such model"):
        await client().complete(system="s", messages=[Message("user", "hi")])
    assert route.call_count == 1, "asking again cannot fix what we sent"


@respx.mock
async def test_a_rate_limit_is_retried() -> None:
    """The one 4xx worth a second go: on a free model it means the shared pool is busy
    this second, not that the request is wrong."""
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(429, json={}), reply({"content": "ok"})]
    )
    result = await client().complete(system="s", messages=[Message("user", "hi")])
    assert result.text == "ok"
    assert route.call_count == 2


@respx.mock
async def test_a_server_error_is_retried_then_reported() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(503, json={}))
    with pytest.raises(LLMUnavailable, match="after a retry"):
        await client().complete(system="s", messages=[Message("user", "hi")])
    assert route.call_count == 2
