"""Composing the passes, and reading the context they all depend on."""

from decimal import Decimal

import pytest

from komora.core.models import DraftBasket, DraftLine
from komora.core.passes.promos import DEGRADED_COUPONS
from komora.core.pipeline import (
    DEGRADED_RESTRICTIONS,
    CartContextMissing,
    build_cart,
    extract_restrictions,
    load_context,
)
from tests.fakes import CONTEXT, FakeSilpo, product


def basket(*descriptions: str) -> DraftBasket:
    return DraftBasket(
        title="Кошик",
        intent="stated",
        lines=[
            DraftLine(description=d, quantity=1, reason_text="ви попросили") for d in descriptions
        ],
    )


CATALOGUE = {
    "молоко": [product("Молоко", 42.90)],
    "хліб": [product("Хліб", 28.50, old_price=35.00)],
    "арахісове масло": [product("Арахісове масло", 99.00)],
}


class TestLoadContext:
    async def test_reads_branch_and_timeslot_off_the_cart(self) -> None:
        """The only place they exist — which is why Silpo calls reading the cart the
        first step."""
        cart_id, context = await load_context(FakeSilpo())
        assert cart_id
        assert context == CONTEXT

    async def test_a_cart_without_shipments_is_reported_distinctly(self) -> None:
        mcp = FakeSilpo()
        original = mcp.get_shopping_cart_by_id

        async def without_shipments(cart_id: str) -> dict:
            payload = await original(cart_id)
            payload["cart"]["shipments"] = []
            return payload

        mcp.get_shopping_cart_by_id = without_shipments  # type: ignore[method-assign]
        with pytest.raises(CartContextMissing, match="branch_id"):
            await load_context(mcp)

    async def test_a_cart_without_a_timeslot_is_reported_distinctly(self) -> None:
        """A real state for a new account, and only the user can fix it."""
        mcp = FakeSilpo()
        original = mcp.get_shopping_cart_by_id

        async def without_timeslot(cart_id: str) -> dict:
            payload = await original(cart_id)
            payload["cart"]["timeslot"] = {}
            return payload

        mcp.get_shopping_cart_by_id = without_timeslot  # type: ignore[method-assign]
        with pytest.raises(CartContextMissing, match="timeslot"):
            await load_context(mcp)

    async def test_a_missing_cart_id_is_reported_distinctly(self) -> None:
        mcp = FakeSilpo()

        async def no_id() -> dict:
            return {"success": True}

        mcp.get_my_shopping_cart = no_id  # type: ignore[method-assign]
        with pytest.raises(CartContextMissing):
            await load_context(mcp)


class TestExtractRestrictions:
    """The response shape is uncaptured — the verified account has none set — so the
    plausible shapes are accepted and anything else reads as empty."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"restrictions": ["арахіс", "лактоза"]},
            {"items": [{"name": "арахіс"}, {"name": "лактоза"}]},
            ["арахіс", "лактоза"],
            {"data": [{"title": "арахіс"}, {"value": "лактоза"}]},
        ],
    )
    def test_plausible_shapes(self, payload: object) -> None:
        assert extract_restrictions(payload) == ["арахіс", "лактоза"]

    @pytest.mark.parametrize("payload", [None, {}, {"restrictions": None}, "текст", 42])
    def test_anything_else_is_empty_rather_than_an_error(self, payload: object) -> None:
        assert extract_restrictions(payload) == []


class TestBuildCart:
    async def test_runs_the_whole_pipeline(self) -> None:
        mcp = FakeSilpo(CATALOGUE)
        cart = await build_cart(basket("молоко", "хліб"), mcp, CONTEXT)
        assert [line.name for line in cart.lines] == ["Молоко", "Хліб"]
        assert cart.total == Decimal("71.40")
        assert cart.estimated_savings == Decimal("6.50"), "35.00 - 28.50 on the bread"

    async def test_restrictions_are_applied_before_anything_is_searched(self) -> None:
        """An excluded line should not cost a search — and must leave a visible trace."""
        mcp = FakeSilpo(CATALOGUE, restrictions={"restrictions": ["арахіс"]})
        cart = await build_cart(basket("молоко", "арахісове масло"), mcp, CONTEXT)

        assert [line.name for line in cart.lines] == ["Молоко"]
        assert any(w.startswith("excluded:") for w in cart.warnings)
        assert mcp.search_calls == [["молоко"]]

    async def test_a_budget_cap_annotates_without_removing_anything(self) -> None:
        mcp = FakeSilpo(CATALOGUE)
        cart = await build_cart(basket("молоко", "хліб"), mcp, CONTEXT, budget_cap=50)
        assert len(cart.lines) == 2, "going over budget is the user's call"
        assert any(w.startswith("over_budget:") for w in cart.warnings)

    async def test_coupons_are_surfaced_as_text(self) -> None:
        """They carry no eligible-product list, so they are shown, never applied."""
        mcp = FakeSilpo(
            CATALOGUE,
            coupons=[{"active": True, "description": "−25% на каву", "limitText": "до неділі"}],
        )
        cart = await build_cart(basket("молоко"), mcp, CONTEXT)
        assert "−25% на каву (до неділі)" in cart.savings_notes
        assert cart.estimated_savings == Decimal("0"), "a coupon is not a computed saving"

    async def test_coupons_failing_degrades_rather_than_fails(self) -> None:
        mcp = FakeSilpo(CATALOGUE, fails={"get_my_coupons"})
        cart = await build_cart(basket("молоко"), mcp, CONTEXT)
        assert [line.name for line in cart.lines] == ["Молоко"]
        assert DEGRADED_COUPONS in cart.warnings

    async def test_restrictions_failing_degrades_rather_than_fails(self) -> None:
        """Worth stating plainly: the cart is still built, and the user is told the
        check did not run."""
        mcp = FakeSilpo(CATALOGUE, fails={"get_my_food_restrictions"})
        cart = await build_cart(basket("молоко"), mcp, CONTEXT)
        assert [line.name for line in cart.lines] == ["Молоко"]
        assert DEGRADED_RESTRICTIONS in cart.warnings

    async def test_a_search_failure_is_not_swallowed(self) -> None:
        """Unlike coupons, this one has no partial answer worth showing."""
        mcp = FakeSilpo(CATALOGUE, fails={"find_products_batch"})
        with pytest.raises(RuntimeError):
            await build_cart(basket("молоко"), mcp, CONTEXT)
