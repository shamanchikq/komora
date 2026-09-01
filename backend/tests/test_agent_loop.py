"""The agent loop.

The LLM decides; it never acts. Read tools are open to it, every write tool is
unreachable, and a basket is only ever *proposed* — resolving it into real products is
the deterministic pipeline's job.
"""

import json
import pathlib

import pytest

from komora.core.agent.loop import ForbiddenToolCall, run_agent
from komora.core.agent.prompts import SYSTEM_PROMPT
from komora.core.agent.tools import (
    INJECTED_PARAMS,
    PROPOSE_BASKET,
    READ_TOOLS,
    build_tool_decls,
)
from komora.core.llm.protocol import LLMResponse, Message, ToolCall
from tests.fakes import CONTEXT, FakeSilpo, product

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "mcp" / "tools.json"
ALL_TOOLS = json.loads(FIXTURE.read_text(encoding="utf-8"))


class FakeLLM:
    """Replays a scripted sequence and records what it was sent."""

    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, *, system, messages, tools=()):
        self.calls.append({"system": system, "messages": list(messages), "tools": list(tools)})
        if not self._responses:
            return LLMResponse(text="(нема що сказати)")
        return self._responses.pop(0)


def basket_call(**overrides: object) -> ToolCall:
    args = {
        "title": "Звичайний кошик",
        "lines": [
            {"description": "молоко 2,6%", "quantity": 2, "reason_text": "ви попросили"},
            {"description": "хліб", "quantity": 1, "reason_text": "ви попросили"},
        ],
    } | overrides
    return ToolCall(PROPOSE_BASKET, args)


async def run(llm: FakeLLM, mcp: FakeSilpo | None = None, **kw: object):
    return await run_agent(
        llm=llm,
        mcp=mcp or FakeSilpo({}),
        context=CONTEXT,
        history=[],
        user_message="Купи молоко і хліб",
        tools=build_tool_decls(ALL_TOOLS),
        **kw,  # type: ignore[arg-type]
    )


class TestToolRegistry:
    def test_no_write_tool_can_reach_the_llm(self) -> None:
        """Checked against the FULL captured tool list, so a future allowlist edit
        that sneaks a mutating tool in fails here rather than in production."""
        exposed = set(READ_TOOLS)
        mutating = {
            t["name"]
            for t in ALL_TOOLS
            if any(
                word in t["name"]
                for word in ("add_", "update_", "remove_", "clear_", "certificate")
            )
        }
        assert mutating, "fixture should contain write tools, or this test proves nothing"
        assert exposed & mutating == set()

    def test_declarations_are_built_from_the_captured_schemas(self) -> None:
        decls = build_tool_decls(ALL_TOOLS)
        names = {d.name for d in decls}
        assert PROPOSE_BASKET in names
        assert "silpo_find_products_batch" in names
        assert not names - (set(READ_TOOLS) | {PROPOSE_BASKET})

    def test_the_model_is_never_told_to_call_a_tool_it_does_not_have(self) -> None:
        """Silpo's own descriptions say "Prefer getting branchId/deliveryType/timeslot
        from silpo_get_shopping_cart_by_id". That tool is not in READ_TOOLS and those
        parameters are stripped, so the advice asks for the impossible."""
        reachable = set(READ_TOOLS) | {PROPOSE_BASKET}
        for decl in build_tool_decls(ALL_TOOLS):
            named = {t["name"] for t in ALL_TOOLS if t["name"] in decl.description}
            assert not named - reachable, f"{decl.name} points at {named - reachable}"

    def test_the_real_advice_survives(self) -> None:
        """Only the unreachable sentence goes — the rest of Silpo's guidance is the
        most reliable documentation this API has."""
        search = next(
            d for d in build_tool_decls(ALL_TOOLS) if d.name == "silpo_find_products_batch"
        )
        assert "article code" in search.description.lower()
        assert "branchId" not in search.description

    def test_context_parameters_are_hidden_from_the_model(self) -> None:
        """Live finding: shown the real schema, the model stalls asking which store and
        delivery slot to use. The loop knows those, so it injects them."""
        search = next(
            d for d in build_tool_decls(ALL_TOOLS) if d.name == "silpo_find_products_batch"
        )
        assert not INJECTED_PARAMS & set(search.parameters["properties"])
        assert not INJECTED_PARAMS & set(search.parameters.get("required", []))
        assert "products" in search.parameters["properties"], "the real argument stays"


class TestProposeBasketSchema:
    def test_is_flat_with_no_refs(self) -> None:
        """Pydantic's model_json_schema emits $defs/$ref for the nested line model.
        Gemini would have to inline them and Gemma degrades on them, so the schema is
        hand-written instead."""
        decl = next(d for d in build_tool_decls(ALL_TOOLS) if d.name == PROPOSE_BASKET)
        text = json.dumps(decl.parameters)
        assert "$ref" not in text and "$defs" not in text

    def test_a_conforming_payload_validates_as_a_draft_basket(self) -> None:
        """Keeps the hand-written schema honest against the model it feeds."""
        from komora.core.models import DraftBasket

        payload = dict(basket_call().args) | {"intent": "stated"}
        assert len(DraftBasket.model_validate(payload).lines) == 2

    def test_every_field_states_its_language(self) -> None:
        """Parameter-value language leakage is the dominant multilingual failure —
        Ukrainian belongs in prose fields and nowhere else."""
        decl = next(d for d in build_tool_decls(ALL_TOOLS) if d.name == PROPOSE_BASKET)
        item = decl.parameters["properties"]["lines"]["items"]["properties"]
        assert "українськ" in item["description"]["description"].lower()
        assert "українськ" in item["reason_text"]["description"].lower()


class TestOutcomes:
    async def test_plain_answer_is_returned_as_a_reply(self) -> None:
        outcome = await run(FakeLLM(LLMResponse(text="У нас є грузинське вино за 420 ₴")))
        assert outcome.reply == "У нас є грузинське вино за 420 ₴"
        assert outcome.basket is None

    async def test_propose_basket_returns_a_draft_without_touching_silpo(self) -> None:
        mcp = FakeSilpo({})
        outcome = await run(FakeLLM(LLMResponse(tool_calls=(basket_call(),))), mcp)
        assert outcome.basket is not None
        assert [ln.description for ln in outcome.basket.lines] == ["молоко 2,6%", "хліб"]
        assert outcome.reply is None
        assert mcp.search_calls == [], "proposing must not resolve anything"

    async def test_read_tool_is_dispatched_and_the_loop_continues(self) -> None:
        mcp = FakeSilpo({"вино": [product("Вино", 420)]})
        llm = FakeLLM(
            LLMResponse(
                tool_calls=(ToolCall("silpo_find_products_batch", {"products": ["вино"]}),)
            ),
            LLMResponse(text="Є «Вино» за 420 ₴"),
        )
        outcome = await run(llm, mcp)
        assert outcome.reply == "Є «Вино» за 420 ₴"
        assert mcp.search_calls == [["вино"]]

    async def test_search_context_is_injected_on_dispatch(self) -> None:
        """The model never supplies branch or timeslot; the loop does."""
        mcp = FakeSilpo({"вино": []})
        llm = FakeLLM(
            LLMResponse(
                tool_calls=(ToolCall("silpo_find_products_batch", {"products": ["вино"]}),)
            ),
            LLMResponse(text="ok"),
        )
        await run(llm, mcp)
        assert mcp.search_calls == [["вино"]]


class TestGuardrails:
    @pytest.mark.parametrize(
        "tool",
        [
            "silpo_add_or_update_cart_products",
            "silpo_remove_cart_products",
            "silpo_clear_shopping_cart",
            "silpo_update_shopping_cart",
        ],
    )
    async def test_write_tool_calls_are_refused(self, tool: str) -> None:
        mcp = FakeSilpo({})
        with pytest.raises(ForbiddenToolCall, match=tool):
            await run(FakeLLM(LLMResponse(tool_calls=(ToolCall(tool, {}),))), mcp)
        assert mcp.search_calls == [] and mcp.replacement_calls == []

    async def test_unknown_tool_is_refused(self) -> None:
        with pytest.raises(ForbiddenToolCall, match="silpo_invented"):
            await run(FakeLLM(LLMResponse(tool_calls=(ToolCall("silpo_invented", {}),))))


class TestLoopSafety:
    async def test_tool_results_reach_the_next_prompt(self) -> None:
        """Not redundant against a fake LLM: the Ollama/llama.cpp Gemma template has
        been reported to drop tool-result messages, so the model never sees output and
        re-calls forever. That presents as stupidity and is an integration bug."""
        mcp = FakeSilpo({"вино": [product("Вино Колоніст", 259)]})
        llm = FakeLLM(
            LLMResponse(
                tool_calls=(ToolCall("silpo_find_products_batch", {"products": ["вино"]}),)
            ),
            LLMResponse(text="done"),
        )
        await run(llm, mcp)
        second_prompt = llm.calls[1]["messages"]
        assert any(m.role == "tool" for m in second_prompt)
        assert "Вино Колоніст" in "".join(m.content for m in second_prompt if m.role == "tool")

    async def test_identical_repeated_call_is_stopped(self) -> None:
        """Cheaper than burning every step on the same call."""
        call = ToolCall("silpo_find_products_batch", {"products": ["вино"]})
        llm = FakeLLM(*[LLMResponse(tool_calls=(call,)) for _ in range(5)])
        outcome = await run(llm, FakeSilpo({"вино": []}))
        assert outcome.reply is not None
        assert len(llm.calls) < 5, "should stop once the call repeats identically"

    async def test_step_limit_ends_with_a_usable_reply(self) -> None:
        calls = [
            LLMResponse(tool_calls=(ToolCall("silpo_find_products_batch", {"products": [f"{i}"]}),))
            for i in range(20)
        ]
        outcome = await run(FakeLLM(*calls), FakeSilpo({}), max_steps=4)
        assert outcome.basket is None
        assert outcome.reply, "a user must never be left with silence"

    async def test_malformed_basket_is_retried_with_an_explanation(self) -> None:
        """Exactly what gemma4:12b produced under tool load: description=null on every
        line, despite the field being required."""
        broken = basket_call(
            lines=[{"description": None, "quantity": 1, "reason_text": "бо треба"}]
        )
        llm = FakeLLM(LLMResponse(tool_calls=(broken,)), LLMResponse(tool_calls=(basket_call(),)))
        outcome = await run(llm)
        assert outcome.basket is not None, "the retry should recover"
        assert len(llm.calls) == 2
        retry_prompt = llm.calls[1]["messages"]
        assert any(m.role == "tool" and "description" in m.content for m in retry_prompt)

    async def test_repeatedly_malformed_basket_gives_up_gracefully(self) -> None:
        broken = basket_call(lines=[{"quantity": 1}])
        outcome = await run(FakeLLM(*[LLMResponse(tool_calls=(broken,)) for _ in range(6)]))
        assert outcome.basket is None
        assert outcome.reply

    async def test_a_guessed_parameter_name_is_retried_not_fatal(self) -> None:
        """Models guess parameter names — every name assumed from a tool name in this
        project turned out wrong. A stray kwarg raised TypeError past every catch
        layer and left the user in silence."""
        llm = FakeLLM(
            LLMResponse(tool_calls=(ToolCall("silpo_get_my_coupons", {"limit": 5}),)),
            LLMResponse(text="Ось купони"),
        )
        outcome = await run(llm, FakeSilpo({}))
        assert outcome.reply == "Ось купони"
        tool_messages = [m.content for m in llm.calls[1]["messages"] if m.role == "tool"]
        assert any("limit" in content for content in tool_messages)

    async def test_the_same_wrong_arguments_repeated_stop_the_loop(self) -> None:
        call = ToolCall("silpo_get_my_coupons", {"limit": 5})
        llm = FakeLLM(*[LLMResponse(tool_calls=(call,)) for _ in range(4)])
        outcome = await run(llm, FakeSilpo({}))
        assert outcome.reply is not None
        assert len(llm.calls) == 2, "the identical-call guard ends it after one retry"


class TestPrompt:
    async def test_history_precedes_the_new_message(self) -> None:
        llm = FakeLLM(LLMResponse(text="ok"))
        await run_agent(
            llm=llm,
            mcp=FakeSilpo({}),
            context=CONTEXT,
            history=[Message("user", "раніше"), Message("assistant", "відповідь")],
            user_message="тепер",
            tools=build_tool_decls(ALL_TOOLS),
        )
        contents = [m.content for m in llm.calls[0]["messages"]]
        assert contents == ["раніше", "відповідь", "тепер"]

    async def test_system_prompt_forbids_searching_for_a_stated_list(self) -> None:
        """Live finding: without this the model asks which store to use instead of
        proposing anything."""
        llm = FakeLLM(LLMResponse(text="ok"))
        await run(llm)
        system = llm.calls[0]["system"].lower()
        assert "не шукай" in system
        assert PROPOSE_BASKET in system


class TestPromptRules:
    """Each rule here answers a failure seen in a live Telegram run."""

    def test_the_schema_forbids_parenthetical_descriptions(self) -> None:
        """«Ковбаса (наприклад, салямі або варена)» matched 0 products; «ковбаса»
        matched 30. resolve.py retries with a simplified term, but not writing one in
        the first place is cheaper than a second round trip."""
        decl = next(d for d in build_tool_decls(ALL_TOOLS) if d.name == PROPOSE_BASKET)
        text = decl.parameters["properties"]["lines"]["items"]["properties"]["description"][
            "description"
        ]
        assert "дужок" in text and "наприклад" in text

    def test_the_prompt_gives_worked_examples_of_good_and_bad_descriptions(self) -> None:
        assert "ДОБРЕ:" in SYSTEM_PROMPT and "ПОГАНО:" in SYSTEM_PROMPT

    def test_the_prompt_pins_the_default_quantity(self) -> None:
        """Asked for milk and eggs it chose three cartons of eggs."""
        assert "quantity — 1" in SYSTEM_PROMPT

    def test_the_prompt_forbids_carrying_items_over_from_earlier_turns(self) -> None:
        """A pizza basket arrived with milk in it, reasoned «за вашим проханням» —
        the request was two turns earlier and the claim was false."""
        assert "Не переноси товари з попередніх" in SYSTEM_PROMPT
        assert "за вашим проханням" in SYSTEM_PROMPT
