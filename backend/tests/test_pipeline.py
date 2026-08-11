"""Composing the passes, and reading the context they all depend on."""

import json
import pathlib
from decimal import Decimal

import pytest

from komora.core.models import DraftBasket, DraftLine
from komora.core.passes.promos import DEGRADED_COUPONS
from komora.core.pipeline import (
    DEGRADED_RESTRICTIONS,
    TIMESLOT_EXPIRED,
    CartContextMissing,
    _listed,
    build_cart,
    extract_restrictions,
    load_context,
    slot_verdict,
    timeslot_is_offered,
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


class TestCapturedEnvelopes:
    """Captured live 2026-08-12, replacing three guesses with facts.

    Each was previously handled by accepting several plausible shapes. The tolerance
    stays — an account with data may still reveal a variation — but these pin what the
    server actually sends, so a future change is a failing test rather than a silent
    empty list.
    """

    def test_coupons_arrive_under_coupons(self) -> None:
        payload = _fixture("my_coupons")
        assert sorted(payload) == ["coupons", "success", "summary"]
        assert _listed(payload, "coupons", "items", "data") == payload["coupons"]

    def test_a_live_coupon_carries_no_discount_value(self) -> None:
        """The headline "swap X for Y to trigger a 40% coupon" feature rests on this,
        and this is the evidence: the real coupon has no value field at all."""
        [coupon] = _fixture("my_coupons")["coupons"]
        assert "rewardValue" not in coupon and "rewardText" not in coupon
        assert coupon["description"] == "на онлайн чек", "a fragment, not a description"

    def test_restrictions_arrive_under_restrictions(self) -> None:
        payload = _fixture("my_food_restrictions")
        assert sorted(payload) == ["restrictions", "success", "summary"]
        assert extract_restrictions(payload) == [], "none set on the captured account"

    def test_time_slots_arrive_under_slots(self) -> None:
        payload = _fixture("time_slots")
        assert "slots" in payload and "timeslots" not in payload
        assert {"start", "end", "available", "deliveryType"} <= set(payload["slots"][0])


class TestExtractRestrictions:
    """Tolerance kept on purpose: the captured account has no restrictions set, so a
    populated response has still never been seen."""

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


FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "mcp"


def _fixture(name: str) -> dict:
    loaded: dict = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return loaded


LIVE_SLOTS = _fixture("time_slots")


def slot(start: str, *, available: bool = True) -> dict:
    return {"start": start, "end": start, "available": available, "deliveryType": "SelfPickup"}


class TestSlotVerdict:
    """Only `available: true` counts — Silpo's own description says so, and the list
    includes slots that have already passed."""

    def test_an_offered_slot_passes(self) -> None:
        payload = {"slots": [slot("2026-08-12T06:00:00+00:00")]}
        assert slot_verdict(payload, "2026-08-12T06:00:00+00:00") is True

    def test_a_passed_slot_is_present_but_unavailable(self) -> None:
        """Observed live at 23:47 UTC: every slot of the current day returned, all
        with `available: false`.

        Written inline rather than against the fixture on purpose — how many slots are
        available depends on the hour the capture ran, and an assertion about that
        would fail whenever someone re-captures in the morning.
        """
        passed = {"slots": [slot("2026-08-11T06:00:00+00:00", available=False)]}
        assert slot_verdict(passed, "2026-08-11T06:00:00+00:00") is False

    def test_the_captured_fixture_still_parses(self) -> None:
        """Whatever hour it was captured at, the verdict must be a real answer."""
        assert slot_verdict(LIVE_SLOTS, LIVE_SLOTS["slots"][0]["start"]) is not None

    def test_an_unreadable_payload_is_not_judged(self) -> None:
        assert slot_verdict({}, "2026-08-12T06:00:00+00:00") is None

    async def test_the_window_starts_at_the_slot_being_checked(self) -> None:
        """The false positive this fixes: asked without `start`, Silpo answers with the
        current day's window, which by evening is entirely in the past — and a valid
        cart slot in tomorrow's window looks expired."""
        mcp = FakeSilpo()
        assert await timeslot_is_offered(mcp, CONTEXT) is True

        without_start = await mcp.get_time_slots(
            branch_id=CONTEXT.branch_id, delivery_type=CONTEXT.delivery_type
        )
        assert without_start["summary"].endswith("(1 available)")
        assert slot_verdict(without_start, "2026-08-11T06:00:00+00:00") is False

    async def test_silpo_failing_never_reads_as_fine(self) -> None:
        mcp = FakeSilpo(fails={"get_time_slots"})
        assert await timeslot_is_offered(mcp, CONTEXT) is None


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
            coupons=[{"id": 1, "active": True, "description": "на онлайн чек"}],
        )
        cart = await build_cart(basket("молоко"), mcp, CONTEXT)
        assert "на онлайн чек" in cart.savings_notes
        assert cart.estimated_savings == Decimal("0"), "a coupon is not a computed saving"

    async def test_a_coupon_is_enriched_with_the_value_the_list_omits(self) -> None:
        """`get_my_coupons` cannot return a discount value; `get_coupon_details` can."""
        mcp = FakeSilpo(
            CATALOGUE,
            coupons=[{"id": 518608454, "active": True, "description": "на онлайн чек"}],
            coupon_details={518608454: {"rewardText": "−10%", "rewardValue": 10}},
        )
        cart = await build_cart(basket("молоко"), mcp, CONTEXT)
        assert any(note.startswith("−10% на онлайн чек") for note in cart.savings_notes)

    async def test_a_failed_enrichment_keeps_the_plain_coupon(self) -> None:
        mcp = FakeSilpo(
            CATALOGUE,
            coupons=[{"id": 7, "active": True, "description": "на онлайн чек"}],
            fails={"get_coupon_details"},
        )
        cart = await build_cart(basket("молоко"), mcp, CONTEXT)
        assert "на онлайн чек" in cart.savings_notes
        assert DEGRADED_COUPONS not in cart.warnings, "the coupon itself still arrived"

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

    async def test_an_expired_timeslot_warns_without_blocking_the_cart(self) -> None:
        """Silpo asks for this check up front. The cart is still worth building — the
        user just has to pick a new slot before checkout."""
        mcp = FakeSilpo(CATALOGUE, slots=[slot(CONTEXT.timeslot_start, available=False)])
        cart = await build_cart(basket("молоко"), mcp, CONTEXT)
        assert [line.name for line in cart.lines] == ["Молоко"]
        assert TIMESLOT_EXPIRED in cart.warnings

    async def test_a_valid_timeslot_adds_no_warning(self) -> None:
        cart = await build_cart(basket("молоко"), FakeSilpo(CATALOGUE), CONTEXT)
        assert TIMESLOT_EXPIRED not in cart.warnings

    async def test_a_search_failure_is_not_swallowed(self) -> None:
        """Unlike coupons, this one has no partial answer worth showing."""
        mcp = FakeSilpo(CATALOGUE, fails={"find_products_batch"})
        with pytest.raises(RuntimeError):
            await build_cart(basket("молоко"), mcp, CONTEXT)
