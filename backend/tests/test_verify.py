"""The verification pass — the only thing that can tell an ice-cream cone from a
pizza base.

Every other check passes a wrong product: it exists, it is in stock, the price is
sane, and its name shares words with the query.
"""

from decimal import Decimal

from komora.core.llm.protocol import LLMResponse, ToolCall
from komora.core.models import DraftBasket, DraftLine, ResolvedLine
from komora.core.passes.verify import (
    DEGRADED_VERIFY,
    REPORT_TOOL,
    find_mismatches,
)
from komora.core.pipeline import build_cart
from tests.fakes import CONTEXT, FakeSilpo, product


def verdicts(*entries: dict) -> LLMResponse:
    return LLMResponse(tool_calls=(ToolCall(REPORT_TOOL, {"results": list(entries)}),))


class OneShotLLM:
    """Answers every call with the same scripted response, and records the prompts."""

    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.prompts: list[str] = []

    async def complete(self, *, system, messages, tools=()):  # type: ignore[no-untyped-def]
        self.prompts.append("\n".join(m.content for m in messages))
        return self._response


class BrokenLLM:
    async def complete(self, **_: object) -> LLMResponse:
        raise RuntimeError("model unavailable")


def line(description: str, name: str) -> ResolvedLine:
    return ResolvedLine(
        description=description,
        product_id=f"id-{name}",
        company_id="c",
        branch_id="b",
        name=name,
        qty=1,
        unit="шт",
        unit_price=Decimal("10"),
        reason_kind="stated",
        reason_text="ви попросили",
    )


class TestFindMismatches:
    async def test_a_clean_basket_reports_nothing(self) -> None:
        llm = OneShotLLM(verdicts({"index": 0, "verdict": "ok"}))
        assert await find_mismatches(llm, [("сир твердий", "Сир Пирятин твердий")]) == {}

    async def test_a_mismatch_carries_a_better_query(self) -> None:
        llm = OneShotLLM(
            verdicts({"index": 0, "verdict": "mismatch", "better_query": "тісто для піци"})
        )
        found = await find_mismatches(llm, [("основа для піци", "Корзинка для морозива")])
        assert found == {0: "тісто для піци"}

    async def test_an_empty_basket_needs_no_call(self) -> None:
        llm = OneShotLLM(verdicts())
        assert await find_mismatches(llm, []) == {}
        assert llm.prompts == [], "no request spent on nothing"

    async def test_one_request_covers_the_whole_basket(self) -> None:
        """Requests, not tokens, are the binding limit on Gemini's free tier."""
        llm = OneShotLLM(verdicts({"index": 1, "verdict": "mismatch", "better_query": "хліб"}))
        pairs = [(f"товар {i}", f"Товар {i}") for i in range(8)]
        await find_mismatches(llm, pairs)
        assert len(llm.prompts) == 1
        assert "товар 7" in llm.prompts[0], "every line is in the one prompt"

    async def test_a_failed_check_is_not_a_pass(self) -> None:
        """`None` and `{}` must never be conflated: a broken verifier reporting a
        clean bill of health is worse than no verifier."""
        assert await find_mismatches(BrokenLLM(), [("молоко", "Молоко")]) is None

    async def test_a_reply_without_the_tool_call_is_not_a_pass(self) -> None:
        assert await find_mismatches(OneShotLLM(LLMResponse(text="ок")), [("a", "b")]) is None

    async def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        llm = OneShotLLM(
            verdicts(
                {"verdict": "mismatch"},  # no index
                {"index": "nonsense", "verdict": "mismatch"},
                {"index": 99, "verdict": "mismatch"},  # out of range
                {"index": 0, "verdict": "mismatch", "better_query": "хліб"},
            )
        )
        assert await find_mismatches(llm, [("хлібчик", "Цукерки")]) == {0: "хліб"}

    async def test_a_json_string_payload_is_parsed(self) -> None:
        """Some providers hand back a string where an array was declared."""
        llm = OneShotLLM(
            LLMResponse(
                tool_calls=(
                    ToolCall(
                        REPORT_TOOL,
                        {"results": '[{"index": 0, "verdict": "mismatch", "better_query": "х"}]'},
                    ),
                )
            )
        )
        assert await find_mismatches(llm, [("a", "b")]) == {0: "х"}


def basket(*descriptions: str) -> DraftBasket:
    return DraftBasket(
        title="Кошик",
        intent="stated",
        lines=[DraftLine(description=d, reason_text="ви попросили") for d in descriptions],
    )


CATALOGUE = {
    "основа для піци": [product("Корзинка Progelcone для морозива", 139)],
    "тісто для піци": [product("Тісто Eesti Pagar для піци", 159)],
    "молоко": [product("Молоко", 42.90)],
}


class TestVerificationInThePipeline:
    async def test_a_flagged_line_is_re_resolved(self) -> None:
        llm = OneShotLLM(
            verdicts({"index": 0, "verdict": "mismatch", "better_query": "тісто для піци"})
        )
        cart = await build_cart(basket("основа для піци"), FakeSilpo(CATALOGUE), CONTEXT, llm=llm)
        assert [ln.name for ln in cart.lines] == ["Тісто Eesti Pagar для піци"]
        assert cart.lines[0].description == "основа для піци", "the ask is preserved"

    async def test_a_flagged_line_with_no_replacement_is_reported(self) -> None:
        """Better an honest «не знайшлося» than a product the user might buy."""
        llm = OneShotLLM(
            verdicts({"index": 0, "verdict": "mismatch", "better_query": "нічого такого"})
        )
        cart = await build_cart(basket("основа для піци"), FakeSilpo(CATALOGUE), CONTEXT, llm=llm)
        assert cart.lines == []
        assert "not_found:основа для піци" in cart.warnings

    async def test_an_approved_basket_is_untouched(self) -> None:
        llm = OneShotLLM(verdicts({"index": 0, "verdict": "ok"}))
        cart = await build_cart(basket("молоко"), FakeSilpo(CATALOGUE), CONTEXT, llm=llm)
        assert [ln.name for ln in cart.lines] == ["Молоко"]
        assert DEGRADED_VERIFY not in cart.warnings

    async def test_a_broken_verifier_degrades_visibly(self) -> None:
        cart = await build_cart(basket("молоко"), FakeSilpo(CATALOGUE), CONTEXT, llm=BrokenLLM())
        assert [ln.name for ln in cart.lines] == ["Молоко"], "the cart still arrives"
        assert DEGRADED_VERIFY in cart.warnings

    async def test_no_llm_means_no_verification_and_no_warning(self) -> None:
        cart = await build_cart(basket("молоко"), FakeSilpo(CATALOGUE), CONTEXT)
        assert [ln.name for ln in cart.lines] == ["Молоко"]
        assert cart.warnings == []

    async def test_the_total_is_recomputed_after_a_swap(self) -> None:
        llm = OneShotLLM(
            verdicts({"index": 0, "verdict": "mismatch", "better_query": "тісто для піци"})
        )
        cart = await build_cart(basket("основа для піци"), FakeSilpo(CATALOGUE), CONTEXT, llm=llm)
        assert cart.total == Decimal("159.00"), "not the 139.00 of the rejected pick"
