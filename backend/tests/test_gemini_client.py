"""GeminiClient.

The underlying SDK client is injected, so these run without an API key or network.
"""

from typing import Any

import pytest
from google.genai import types

from komora.core.llm.gemini.client import GeminiClient
from komora.core.llm.protocol import LLMUnavailable, Message, ToolCall, ToolDecl

SEARCH = ToolDecl(
    name="silpo_find_products_batch",
    description="Search several products at once",
    parameters={
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "branchId": {"type": "string", "format": "uuid", "description": "Branch"},
            "products": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
            "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        },
        "required": ["branchId", "products"],
    },
)


class FakeModels:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeSdk:
    def __init__(self, *responses: Any) -> None:
        self.models = FakeModels(list(responses))
        self.aio = self


def reply(text: str | None = None, calls: tuple[types.FunctionCall, ...] = ()) -> Any:
    class R:
        pass

    r = R()
    r.text = text  # type: ignore[attr-defined]
    r.function_calls = list(calls)  # type: ignore[attr-defined]
    return r


def client(*responses: Any) -> tuple[GeminiClient, FakeSdk]:
    sdk = FakeSdk(*responses)
    return GeminiClient(model="gemini-3.1-flash-lite", api_key="x", sdk=sdk), sdk


class TestRequestShape:
    async def test_model_and_system_instruction_are_sent(self) -> None:
        llm, sdk = client(reply("привіт"))
        await llm.complete(system="Ти — Комора.", messages=[Message("user", "привіт")])
        sent = sdk.models.calls[0]
        assert sent["model"] == "gemini-3.1-flash-lite"
        assert sent["config"].system_instruction == "Ти — Комора."

    async def test_tool_schemas_go_through_the_gemini_converter(self) -> None:
        """Raw MCP schema must not reach the wire: $schema and format:uuid are rejected."""
        llm, sdk = client(reply("ok"))
        await llm.complete(system="s", messages=[Message("user", "hi")], tools=[SEARCH])
        declared = sdk.models.calls[0]["config"].tools[0].function_declarations[0]
        params = declared.parameters.model_dump(exclude_none=True)
        assert "$schema" not in params
        assert params["properties"]["branchId"].get("format") is None
        assert params["properties"]["limit"]["nullable"] is True
        assert params["properties"]["products"]["type"].upper() == "ARRAY"

    async def test_automatic_function_calling_is_disabled(self) -> None:
        """AFC hides the loop — no per-step logging and no confirmation before a
        side-effectful call."""
        llm, sdk = client(reply("ok"))
        await llm.complete(system="s", messages=[Message("user", "hi")], tools=[SEARCH])
        assert sdk.models.calls[0]["config"].automatic_function_calling.disable is True

    async def test_temperature_is_never_set(self) -> None:
        """Google warns that changing it on Gemini 3 risks looping or degradation, and
        it is deprecated on 3.6 — the usual temperature=0 reflex is wrong here."""
        llm, sdk = client(reply("ok"))
        await llm.complete(system="s", messages=[Message("user", "hi")])
        assert sdk.models.calls[0]["config"].temperature is None

    async def test_thinking_level_is_explicit(self) -> None:
        """It defaults to `high`, which bills thinking tokens at the output rate."""
        llm, sdk = client(reply("ok"))
        await llm.complete(system="s", messages=[Message("user", "hi")])
        assert sdk.models.calls[0]["config"].thinking_config.thinking_level is not None

    async def test_prefix_is_byte_stable_across_calls(self) -> None:
        """Implicit caching only hits when the leading bytes are identical."""
        llm, sdk = client(reply("a"), reply("b"))
        for text in ("перше", "друге"):
            await llm.complete(system="s", messages=[Message("user", text)], tools=[SEARCH])
        first, second = (c["config"] for c in sdk.models.calls)
        assert first.system_instruction == second.system_instruction
        assert first.tools[0].model_dump_json() == second.tools[0].model_dump_json()


class TestConversation:
    async def test_roles_are_mapped(self) -> None:
        llm, sdk = client(reply("ok"))
        await llm.complete(
            system="s",
            messages=[Message("user", "купи молоко"), Message("assistant", "гаразд")],
        )
        contents = sdk.models.calls[0]["contents"]
        assert [c.role for c in contents] == ["user", "model"], "assistant maps to 'model'"

    async def test_tool_results_are_sent_back_with_their_id(self) -> None:
        """Part.from_function_response has no id parameter despite the docs; the
        FunctionResponse must be built directly or parallel calls cannot be matched."""
        llm, sdk = client(reply("ok"))
        await llm.complete(
            system="s",
            messages=[
                Message("user", "hi"),
                Message("assistant", tool_calls=(ToolCall("t", {"a": 1}, id="call-1"),)),
                Message("tool", '{"ok": true}', tool_name="t", tool_call_id="call-1"),
            ],
        )
        contents = sdk.models.calls[0]["contents"]
        response_part = contents[-1].parts[0].function_response
        assert response_part.name == "t"
        assert response_part.id == "call-1"


class TestResponses:
    async def test_plain_text(self) -> None:
        llm, _ = client(reply("Ось ваш кошик"))
        result = await llm.complete(system="s", messages=[Message("user", "hi")])
        assert result.text == "Ось ваш кошик"
        assert result.tool_calls == ()
        assert result.wants_tools is False

    async def test_function_calls_are_returned(self) -> None:
        llm, _ = client(
            reply(
                None, (types.FunctionCall(id="c1", name="silpo_get_products", args={"q": "сир"}),)
            )
        )
        result = await llm.complete(system="s", messages=[Message("user", "hi")], tools=[SEARCH])
        assert result.wants_tools is True
        assert result.tool_calls[0].name == "silpo_get_products"
        assert result.tool_calls[0].args == {"q": "сир"}
        assert result.tool_calls[0].id == "c1"

    async def test_missing_args_become_an_empty_dict(self) -> None:
        llm, _ = client(reply(None, (types.FunctionCall(name="t", args=None),)))
        result = await llm.complete(system="s", messages=[Message("user", "hi")])
        assert result.tool_calls[0].args == {}


class TestFailures:
    async def test_transient_error_is_retried_once(self) -> None:
        llm, sdk = client(RuntimeError("503 backend"), reply("recovered"))
        assert (await llm.complete(system="s", messages=[Message("user", "hi")])).text == (
            "recovered"
        )
        assert len(sdk.models.calls) == 2

    async def test_persistent_failure_raises_llm_unavailable(self) -> None:
        llm, sdk = client(RuntimeError("503 backend"), RuntimeError("503 backend"))
        with pytest.raises(LLMUnavailable):
            await llm.complete(system="s", messages=[Message("user", "hi")])
        assert len(sdk.models.calls) == 2, "one retry, not an unbounded loop"


def reply_with_parts(*parts: types.Part, text: str | None = None) -> Any:
    """A response shaped like the real SDK's: candidates -> content -> parts.

    `reply()` above only implements the `function_calls` accessor, which is what the
    convenience path reads. The signature lives on the Part, so the bug this models is
    invisible to any fake that skips the part structure.
    """

    class R:
        pass

    r = R()
    r.text = text  # type: ignore[attr-defined]
    r.function_calls = [p.function_call for p in parts if p.function_call]  # type: ignore[attr-defined]
    r.candidates = [  # type: ignore[attr-defined]
        types.Candidate(content=types.Content(role="model", parts=list(parts)))
    ]
    return r


SIGNATURE = b"opaque-thought-signature"


def signed_call(name: str = "silpo_find_products_batch") -> types.Part:
    return types.Part(
        function_call=types.FunctionCall(id="c1", name=name, args={"products": ["вино"]}),
        thought_signature=SIGNATURE,
    )


class TestThoughtSignature:
    """Gemini 3 rejects the request *after* a tool call unless the signature that came
    with it is replayed: `400 INVALID_ARGUMENT: Function call is missing a thought
    signature`. Single-turn calls look perfectly healthy, so this only shows up once a
    tool is actually used — which is how it reached a live run.
    """

    async def test_the_signature_is_captured_from_the_part(self) -> None:
        llm, _ = client(reply_with_parts(signed_call()))
        response = await llm.complete(system="s", messages=[Message("user", "вино?")])
        assert response.tool_calls[0].provider_state == SIGNATURE

    async def test_the_signature_is_replayed_on_the_next_request(self) -> None:
        llm, sdk = client(reply_with_parts(signed_call()), reply(text="ось вино"))

        first = await llm.complete(system="s", messages=[Message("user", "вино?")])
        await llm.complete(
            system="s",
            messages=[
                Message("user", "вино?"),
                Message("assistant", tool_calls=first.tool_calls),
                Message("tool", "[]", tool_name="silpo_find_products_batch", tool_call_id="c1"),
            ],
        )

        sent = sdk.models.calls[1]["contents"]
        model_turn = next(c for c in sent if c.role == "model")
        assert model_turn.parts[0].thought_signature == SIGNATURE

    async def test_a_call_without_a_signature_still_works(self) -> None:
        """Not every part carries one, and inventing a value would be worse."""
        llm, sdk = client(
            reply_with_parts(
                types.Part(function_call=types.FunctionCall(id="c1", name="t", args={}))
            ),
            reply(text="ok"),
        )
        first = await llm.complete(system="s", messages=[Message("user", "?")])
        assert first.tool_calls[0].provider_state is None

        await llm.complete(
            system="s",
            messages=[Message("assistant", tool_calls=first.tool_calls)],
        )
        model_turn = next(c for c in sdk.models.calls[1]["contents"] if c.role == "model")
        assert model_turn.parts[0].thought_signature is None

    async def test_a_response_without_candidates_falls_back_to_the_accessor(self) -> None:
        """Losing the calls entirely would be worse than losing the signature."""
        llm, _ = client(reply(calls=(types.FunctionCall(id="c1", name="t", args={}),)))
        response = await llm.complete(system="s", messages=[Message("user", "?")])
        assert [c.name for c in response.tool_calls] == ["t"]
