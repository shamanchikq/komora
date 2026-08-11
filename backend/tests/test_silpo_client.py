"""The concrete Silpo client.

Everything asserted here is an argument *name* or a response *shape*, because that is
where this project has actually lost time: every parameter name inferred from a tool
name turned out to be wrong. The expected names come from
`tests/fixtures/mcp/tools.json`, which is what the live server publishes.
"""

import json
import pathlib
from typing import Any

import pytest

from komora.core.mcp.client import RetryPolicy
from komora.core.mcp.errors import RateLimited
from komora.core.mcp.protocol import SilpoClient
from komora.core.mcp.silpo import SilpoSession, ToolFailed
from tests.fakes import CONTEXT

SCHEMAS = {
    tool["name"]: tool
    for tool in json.loads(
        (pathlib.Path(__file__).parent / "fixtures" / "mcp" / "tools.json").read_text(
            encoding="utf-8"
        )
    )
}

FAST = RetryPolicy(attempts=2, base_delay=0, jitter=0)


class FakeResult:
    def __init__(self, structured: Any = None, text: str | None = None) -> None:
        self.structured_content = structured
        self.content = [type("Block", (), {"text": text})()] if text is not None else []


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} description"
        self.input_schema = {"type": "object"}


class FakeSession:
    """Stands in for an initialised MCP ClientSession."""

    def __init__(self, *replies: Any, tools: list[str] | None = None) -> None:
        self._replies = list(replies) or [FakeResult({"success": True})]
        self._tools = tools or ["silpo_find_products_batch"]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def list_tools(self) -> Any:
        return type("Listing", (), {"tools": [FakeTool(n) for n in self._tools]})()

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1][1]


def declared(tool: str) -> set[str]:
    return set((SCHEMAS[tool]["inputSchema"].get("properties") or {}).keys())


class TestArgumentNames:
    """Each call sends what the published schema declares — nothing invented."""

    async def test_search_sends_products_and_the_whole_context(self) -> None:
        session = FakeSession()
        await SilpoSession(session).find_products_batch(["молоко", "хліб"], CONTEXT)
        name, args = session.calls[0]
        assert name == "silpo_find_products_batch"
        assert args["products"] == ["молоко", "хліб"], "not `queries`"
        assert args["branchId"] == CONTEXT.branch_id
        assert args["deliveryType"] == CONTEXT.delivery_type
        assert args["timeslotStart"] and args["timeslotEnd"]
        assert not set(args) - declared("silpo_find_products_batch")

    async def test_replacements_sends_product_ids_and_no_timeslot(self) -> None:
        """The one search-ish tool whose schema omits the timeslot entirely."""
        session = FakeSession()
        await SilpoSession(session).get_replacements(
            product_ids=["p1"], company_id="c1", context=CONTEXT
        )
        args = session.last
        assert args == {
            "branchId": CONTEXT.branch_id,
            "deliveryType": CONTEXT.delivery_type,
            "companyId": "c1",
            "productIds": ["p1"],
        }
        assert set(args) == set(SCHEMAS["silpo_get_replacements"]["inputSchema"]["required"])

    async def test_time_slots_sends_delivery_types_as_a_list(self) -> None:
        """Plural and an array here, singular and a string everywhere else."""
        session = FakeSession()
        await SilpoSession(session).get_time_slots(branch_id="b1", delivery_type="SelfPickup")
        assert session.last["deliveryTypes"] == ["SelfPickup"]
        assert not set(session.last) - declared("silpo_get_time_slots")

    async def test_cart_calls_use_shopping_cart_id(self) -> None:
        session = FakeSession()
        await SilpoSession(session).get_shopping_cart_by_id("cart-1")
        assert session.last == {"shoppingCartId": "cart-1"}, "not `cartId`"

    async def test_categories_needs_only_the_branch(self) -> None:
        session = FakeSession()
        await SilpoSession(session).get_categories(CONTEXT, limit=5)
        assert session.last == {"branchId": CONTEXT.branch_id, "limit": 5}

    async def test_product_details_carries_the_slug_and_the_context(self) -> None:
        session = FakeSession()
        await SilpoSession(session).get_product_details("moloko-2-6", CONTEXT)
        assert session.last["slug"] == "moloko-2-6"
        assert not set(SCHEMAS["silpo_get_product_details"]["inputSchema"]["required"]) - set(
            session.last
        )


class TestCartWrites:
    """The one call whose failure costs the user their basket."""

    async def test_only_the_declared_fields_are_sent(self) -> None:
        session = FakeSession()
        await SilpoSession(session).add_or_update_cart_products(
            "cart-1",
            [{"productId": "p", "companyId": "c", "branchId": "b", "quantity": 2, "name": "X"}],
        )
        sent = session.last["products"][0]
        assert sent == {"productId": "p", "companyId": "c", "branchId": "b", "quantity": 2}
        assert "name" not in sent, "not in the schema; never sent to the live server"

    async def test_add_quantity_is_never_set(self) -> None:
        """Silpo offers `addQuantity: true` to sum instead of replace. Komora relies on
        replacing — that is what makes a retried sync idempotent."""
        session = FakeSession()
        await SilpoSession(session).add_or_update_cart_products(
            "cart-1", [{"productId": "p", "companyId": "c", "branchId": "b", "quantity": 1}]
        )
        assert "addQuantity" not in session.last["products"][0]

    async def test_an_incomplete_item_is_refused_before_it_reaches_silpo(self) -> None:
        session = FakeSession()
        with pytest.raises(ValueError, match="companyId"):
            await SilpoSession(session).add_or_update_cart_products(
                "cart-1", [{"productId": "p", "branchId": "b", "quantity": 1}]
            )
        assert session.calls == []


class TestResponses:
    async def test_structured_content_is_preferred(self) -> None:
        session = FakeSession(FakeResult({"success": True, "shoppingCartId": "c-1"}))
        assert (await SilpoSession(session).get_my_shopping_cart())["shoppingCartId"] == "c-1"

    async def test_json_text_content_is_parsed_when_there_is_no_structured_payload(self) -> None:
        session = FakeSession(FakeResult(text=json.dumps({"success": True, "cart": {"id": "x"}})))
        assert (await SilpoSession(session).get_shopping_cart_by_id("x"))["cart"] == {"id": "x"}

    async def test_a_non_mapping_payload_is_wrapped_rather_than_dropped(self) -> None:
        session = FakeSession(FakeResult(["a", "b"]))
        assert (await SilpoSession(session).get_my_coupons())["result"] == ["a", "b"]


class TestFailures:
    async def test_an_mcp_error_string_raises(self) -> None:
        """It arrives as an ordinary truthy string, so it has to be classified."""
        session = FakeSession(FakeResult(text="MCP error -32602: Invalid arguments"))
        with pytest.raises(ToolFailed, match="-32602"):
            await SilpoSession(session).get_my_shopping_cart()

    async def test_success_false_raises(self) -> None:
        session = FakeSession(FakeResult({"success": False, "message": "ні"}))
        with pytest.raises(ToolFailed):
            await SilpoSession(session).get_my_shopping_cart()

    async def test_a_rate_limit_is_retried(self) -> None:
        session = FakeSession(RateLimited(0), FakeResult({"success": True}), tools=[])
        await SilpoSession(session, policy=FAST).get_my_shopping_cart()
        assert len(session.calls) == 2

    async def test_a_validation_failure_is_not_retried(self) -> None:
        """Nothing about it will differ on the second attempt."""
        session = FakeSession(FakeResult(text="MCP error -32602: Invalid arguments"))
        with pytest.raises(ToolFailed):
            await SilpoSession(session, policy=FAST).get_my_shopping_cart()
        assert len(session.calls) == 1


class TestIntrospection:
    async def test_list_tools_reads_the_snake_case_attributes(self) -> None:
        """`tool.inputSchema` raises AttributeError — the camelCase names are pydantic
        aliases, not attributes."""
        session = FakeSession(tools=["silpo_get_categories"])
        [tool] = await SilpoSession(session).list_tools()
        assert tool["name"] == "silpo_get_categories"
        assert tool["inputSchema"] == {"type": "object"}


def test_every_protocol_method_is_implemented() -> None:
    """Catches drift between the Protocol and the client that has to satisfy it —
    which mypy would only notice at a call site that passes one for the other."""
    required = {
        name
        for name in vars(SilpoClient)
        if not name.startswith("_") and callable(getattr(SilpoClient, name, None))
    }
    assert required, "protocol should declare methods, or this test proves nothing"
    assert not required - set(dir(SilpoSession))
