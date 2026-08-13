"""The deterministic pipeline: restrictions, resolve, savings, budget.

Every intent converges here, so each pass is tested on its own rather than through a
model. Product shapes come from the real captured search response.
"""

from decimal import Decimal

import pytest

from komora.core.models import DraftBasket, DraftLine, ResolvedCart, ResolvedLine
from komora.core.passes.budget import apply_budget, optional_lines_total
from komora.core.passes.promos import apply_savings, describe_coupons
from komora.core.passes.resolve import clamp_quantity, fallback_terms, resolve_basket
from komora.core.passes.restrictions import apply_restrictions
from tests.fakes import CONTEXT, FakeSilpo, product


def draft(*lines: DraftLine, title: str = "Кошик") -> DraftBasket:
    return DraftBasket(title=title, intent="stated", lines=list(lines))


def line(description: str, quantity: float = 1, **kw: object) -> DraftLine:
    return DraftLine.model_validate(
        {"description": description, "quantity": quantity, "reason_text": "ви попросили"} | kw
    )


def resolved(**kw: object) -> ResolvedLine:
    base = {
        "product_id": "p1",
        "company_id": "c1",
        "branch_id": "b1",
        "name": "Молоко",
        "qty": 1,
        "unit": "900 мл",
        "unit_price": Decimal("42.90"),
        "reason_kind": "stated",
        "reason_text": "r",
    }
    return ResolvedLine.model_validate(base | kw)


class TestRestrictions:
    def test_no_restrictions_is_identity(self) -> None:
        basket = draft(line("молоко"), line("арахісова паста"))
        assert apply_restrictions(basket, []) == basket

    def test_matching_line_is_dropped_with_a_reason(self) -> None:
        """A line that vanishes without explanation reads as a bug — and for an
        allergy the reason matters more than the item."""
        out = apply_restrictions(draft(line("молоко"), line("Арахісова паста")), ["арахіс"])
        assert [ln.description for ln in out.lines] == ["молоко"]
        assert out.warnings == ["excluded:Арахісова паста:арахіс"]

    def test_matching_is_case_insensitive(self) -> None:
        out = apply_restrictions(draft(line("ЛАКТОЗА free молоко")), ["лактоза"])
        assert out.lines == []

    def test_blank_restrictions_are_ignored(self) -> None:
        basket = draft(line("молоко"))
        assert apply_restrictions(basket, ["", "   "]).lines == basket.lines

    def test_input_is_not_mutated(self) -> None:
        basket = draft(line("арахіс"))
        apply_restrictions(basket, ["арахіс"])
        assert len(basket.lines) == 1


class TestClampQuantity:
    def test_respects_stock(self) -> None:
        assert clamp_quantity(10, product("x", 1, stock=3)) == 3

    def test_rounds_to_the_product_step(self) -> None:
        """Weighted goods are not orderable at arbitrary amounts."""
        assert clamp_quantity(1.0, product("x", 1, step=0.5, stock=None)) == 1.0
        assert clamp_quantity(0.7, product("x", 1, step=0.5, stock=None)) == 0.5

    def test_never_returns_zero(self) -> None:
        assert clamp_quantity(0.1, product("x", 1, step=1, stock=None)) == 1

    def test_step_result_still_capped_by_stock(self) -> None:
        """Two, not three: three was never orderable for a step-2 product, and the old
        nearest-step rounding only reached it by overshooting and then clipping."""
        assert clamp_quantity(9, product("x", 1, step=2, stock=3)) == 2

    def test_a_countable_quantity_never_rounds_up(self) -> None:
        """«велика кола зеро» arrived as quantity=1.5 — the model putting litres in a
        field that counts bottles — and nearest-step rounding bought a second one."""
        assert clamp_quantity(1.5, product("x", 1, step=1, stock=None)) == 1
        assert clamp_quantity(2.9, product("x", 1, step=1, stock=None)) == 2

    def test_a_weighted_quantity_still_rounds_to_the_nearest_step(self) -> None:
        """0,17 kg of cheese is a rounding, not an extra item."""
        cheese = product("x", 1, step=0.1, stock=None)
        cheese["weighted"] = True
        assert clamp_quantity(0.17, cheese) == 0.2


class TestResolve:
    async def test_descriptions_become_real_products(self) -> None:
        mcp = FakeSilpo({"молоко": [product("Молоко Яготинське", 42.90)]})
        cart = await resolve_basket(draft(line("молоко", 2)), mcp, CONTEXT)
        assert len(cart.lines) == 1
        assert cart.lines[0].name == "Молоко Яготинське"
        assert cart.lines[0].qty == 2
        assert cart.total == Decimal("85.80")

    async def test_search_id_becomes_the_cart_product_id(self) -> None:
        """Search says `id`, the cart wants `productId` — the trap from spec 3.1."""
        mcp = FakeSilpo({"молоко": [product("Молоко", 10, product_id="the-uuid")]})
        cart = await resolve_basket(draft(line("молоко")), mcp, CONTEXT)
        assert cart.lines[0].product_id == "the-uuid"

    async def test_quantity_is_capped_at_stock(self) -> None:
        mcp = FakeSilpo({"молоко": [product("Молоко", 10, stock=2)]})
        cart = await resolve_basket(draft(line("молоко", 5)), mcp, CONTEXT)
        assert cart.lines[0].qty == 2

    async def test_plastic_bags_are_never_selected(self) -> None:
        """Silpo's own tool descriptions insist on this."""
        mcp = FakeSilpo({"пакет": [product("Пакет-майка", 2)]})
        cart = await resolve_basket(draft(line("пакет")), mcp, CONTEXT)
        assert cart.lines == []
        assert cart.warnings == ["not_found:пакет"]

    async def test_missing_product_is_reported_not_silently_dropped(self) -> None:
        cart = await resolve_basket(draft(line("ікра")), FakeSilpo({}), CONTEXT)
        assert cart.lines == []
        assert "not_found:ікра" in cart.warnings

    async def test_out_of_stock_is_substituted_and_labelled(self) -> None:
        original = product("Йогурт Активіа", 34.50, product_id="orig", stock=0)
        mcp = FakeSilpo(
            {"йогурт": [original]},
            {"orig": [product("Йогурт Галичина", 31.90, product_id="sub")]},
        )
        cart = await resolve_basket(draft(line("йогурт")), mcp, CONTEXT)
        assert cart.lines[0].name == "Йогурт Галичина"
        assert cart.lines[0].substituted_from == "Йогурт Активіа"
        assert cart.lines[0].reason_kind == "sub"

    async def test_unsubstitutable_line_stays_visible_but_out_of_the_total(self) -> None:
        mcp = FakeSilpo({"ікра": [product("Ікра", 900, product_id="x", stock=0)]}, {"x": []})
        cart = await resolve_basket(draft(line("ікра")), mcp, CONTEXT)
        assert cart.lines[0].unavailable is True
        assert cart.total == Decimal("0"), "unavailable lines must not inflate the total"

    async def test_replacement_failure_degrades_rather_than_fails(self) -> None:
        mcp = FakeSilpo(
            {"йогурт": [product("Йогурт", 30, product_id="o", stock=0)]}, replacements_fail=True
        )
        cart = await resolve_basket(draft(line("йогурт")), mcp, CONTEXT)
        assert "degraded:replacements" in cart.warnings
        assert cart.lines[0].unavailable is True

    async def test_searches_are_batched_within_silpo_limit(self) -> None:
        """30 per call, and the retry pass is deduplicated — 35 failed descriptions
        share one fallback term, so they cost one extra query, not 35."""
        mcp = FakeSilpo({})
        await resolve_basket(draft(*[line(f"товар {i}") for i in range(35)]), mcp, CONTEXT)
        assert [len(c) for c in mcp.search_calls] == [30, 5, 1]
        assert mcp.search_calls[-1] == ["товар"]

    async def test_restriction_warnings_survive_resolution(self) -> None:
        basket = apply_restrictions(draft(line("молоко"), line("арахіс")), ["арахіс"])
        mcp = FakeSilpo({"молоко": [product("Молоко", 10)]})
        cart = await resolve_basket(basket, mcp, CONTEXT)
        assert any(w.startswith("excluded:") for w in cart.warnings)

    async def test_old_price_is_carried_through(self) -> None:
        mcp = FakeSilpo({"молоко": [product("Молоко", 39.99, old_price=60.99)]})
        cart = await resolve_basket(draft(line("молоко")), mcp, CONTEXT)
        assert cart.lines[0].old_price == Decimal("60.99")


class TestSavings:
    def test_discount_comes_from_the_price_difference(self) -> None:
        """The only machine-readable discount Silpo exposes."""
        cart = ResolvedCart(
            lines=[resolved(unit_price=Decimal("39.99"), old_price=Decimal("60.99"), qty=2)]
        )
        out = apply_savings(cart)
        assert out.estimated_savings == Decimal("42.00")
        assert len(out.savings_notes) == 1

    def test_undiscounted_lines_contribute_nothing(self) -> None:
        assert apply_savings(ResolvedCart(lines=[resolved()])).estimated_savings == Decimal("0")

    def test_unavailable_lines_are_excluded(self) -> None:
        cart = ResolvedCart(
            lines=[resolved(unit_price=Decimal("1"), old_price=Decimal("5"), unavailable=True)]
        )
        assert apply_savings(cart).estimated_savings == Decimal("0")

    def test_old_price_below_current_is_ignored(self) -> None:
        cart = ResolvedCart(lines=[resolved(unit_price=Decimal("10"), old_price=Decimal("8"))])
        assert apply_savings(cart).estimated_savings == Decimal("0")

    def test_the_note_is_written_as_money_not_as_a_raw_decimal(self) -> None:
        """Decimal keeps its operands' scale, so this reached a live run as
        «знижка 15.000 ₴»."""
        cart = ResolvedCart(
            lines=[resolved(unit_price=Decimal("47.99"), old_price=Decimal("62.99"))]
        )
        [note] = apply_savings(cart).savings_notes
        assert note.endswith("знижка 15,00 ₴"), note


LIVE_COUPON = {
    "id": 518608454,
    "active": True,
    "useWay": "Електронний",
    "beginDate": "2026-08-04",
    "endDate": "2026-08-31",
    "description": "на онлайн чек",
    "limitText": (
        "• Не діє на подарункові сертифікати,  тютюнові вироби та стартові пакети\r\n"
        "• Пропозиція не діє на доставку LOKO.\r\n"
        "• Діє лише при замовленні доставки на silpo.ua або в застосунку."
    ),
    "warningText": "Максимальна знижка 100 грн",
    "image": "https://content.silpo.ua/promo/example.png",
}
"""The real coupon on the account Task 14 ran against, verbatim.

Note what is *not* here: no `rewardValue`, no `rewardText`. `get_my_coupons` is
`additionalProperties: false` without them, so they can never appear.
"""


class TestCoupons:
    def test_a_real_coupon_reads_as_one_line(self) -> None:
        """The first version inlined limitText and produced three lines of bullets
        inside a parenthesis — caught by the live run, not by a unit test."""
        [note] = describe_coupons([LIVE_COUPON])
        assert note == "на онлайн чек — Максимальна знижка 100 грн"
        assert "\n" not in note and "\r" not in note
        assert "•" not in note

    def test_the_reward_leads_when_details_supplied_it(self) -> None:
        """`rewardText` only exists once a coupon has been enriched from
        get_coupon_details — the list endpoint cannot carry it."""
        [note] = describe_coupons([{**LIVE_COUPON, "rewardText": "−10%"}])
        assert note.startswith("−10% на онлайн чек")

    def test_inactive_coupons_are_skipped(self) -> None:
        assert describe_coupons([{"active": False, "description": "x"}]) == []

    def test_a_coupon_with_no_text_at_all_is_skipped(self) -> None:
        assert describe_coupons([{"active": True, "description": None}]) == []

    def test_coupons_are_not_matched_to_cart_lines(self) -> None:
        """Silpo publishes no coupon-to-product mapping; claiming one would be
        invention. The signature takes no cart, which is the point."""
        import inspect

        assert "cart" not in inspect.signature(describe_coupons).parameters


class TestBudget:
    def test_no_cap_is_identity(self) -> None:
        cart = ResolvedCart(lines=[resolved()], total=Decimal("500"))
        assert apply_budget(cart, None) == cart

    def test_under_cap_is_unchanged(self) -> None:
        cart = ResolvedCart(lines=[resolved()], total=Decimal("500"))
        assert apply_budget(cart, 2000).warnings == []

    def test_over_cap_is_flagged_with_the_overage(self) -> None:
        cart = ResolvedCart(lines=[resolved()], total=Decimal("2400"))
        assert apply_budget(cart, 2000).warnings == ["over_budget:400"]

    def test_nothing_is_removed_when_over_budget(self) -> None:
        """Going over is the user's decision; the UI offers to trim and says so."""
        cart = ResolvedCart(lines=[resolved(), resolved(optional=True)], total=Decimal("2400"))
        assert len(apply_budget(cart, 2000).lines) == 2

    def test_spend_so_far_counts_toward_the_cap(self) -> None:
        cart = ResolvedCart(lines=[resolved()], total=Decimal("500"))
        assert apply_budget(cart, 2000, already_spent=Decimal("1800")).warnings == [
            "over_budget:300"
        ]

    def test_optional_total_is_what_trimming_would_save(self) -> None:
        cart = ResolvedCart(
            lines=[
                resolved(unit_price=Decimal("100"), qty=2, optional=True),
                resolved(unit_price=Decimal("50")),
                resolved(unit_price=Decimal("999"), optional=True, unavailable=True),
            ]
        )
        assert optional_lines_total(cart) == Decimal("200")


class TestPipelineOrder:
    async def test_full_pipeline_produces_a_coherent_cart(self) -> None:
        basket = draft(
            line("молоко", 2),
            line("арахісова паста"),
            line("торт", optional=True),
        )
        mcp = FakeSilpo(
            {
                "молоко": [product("Молоко", 39.99, old_price=60.99)],
                "торт": [product("Торт", 219.00)],
            }
        )
        filtered = apply_restrictions(basket, ["арахіс"])
        cart = await resolve_basket(filtered, mcp, CONTEXT)
        cart = apply_savings(cart)
        cart = apply_budget(cart, 200)

        assert [ln.name for ln in cart.lines] == ["Молоко", "Торт"]
        assert cart.estimated_savings == Decimal("42.00")
        assert any(w.startswith("excluded:") for w in cart.warnings)
        assert any(w.startswith("over_budget:") for w in cart.warnings)

    @pytest.mark.parametrize("cap", [None, 10_000])
    async def test_pipeline_is_clean_when_nothing_goes_wrong(self, cap: int | None) -> None:
        mcp = FakeSilpo({"молоко": [product("Молоко", 39.99)]})
        cart = apply_budget(
            apply_savings(await resolve_basket(draft(line("молоко")), mcp, CONTEXT)), cap
        )
        assert cart.warnings == []


class TestFallbackTerms:
    """Measured live: «Ковбаса (наприклад, салямі або варена)» returns 0 products,
    «ковбаса» returns 30. A parenthetical aside is not worth losing a line over."""

    def test_a_parenthetical_aside_is_dropped_first(self) -> None:
        assert fallback_terms("Ковбаса (наприклад, салямі або варена)")[0] == "Ковбаса"

    def test_then_the_head_word(self) -> None:
        assert fallback_terms("Сир твердий (наприклад, моцарела)") == ["Сир твердий", "Сир"]

    def test_a_plain_description_still_gets_a_head_word(self) -> None:
        assert fallback_terms("Печиво або цукерки") == ["Печиво"]

    def test_a_single_word_has_nothing_simpler(self) -> None:
        assert fallback_terms("молоко") == []


class TestSilpoOrderingIsTrusted:
    """A word-overlap score with a cheapest-wins tie-break was tried and reverted.

    It fixed «яйця» (Silpo returns guinea fowl at 257,40 ₴ ahead of hen's eggs at
    59,39 ₴) and broke «кока кола» (the drink is written "Coca-Cola" in Latin, so
    Cyrillic «кола» matched a 12,99 ₴ marmalade instead). The eggs case was a vague
    query, fixed in the prompt; two alphabets in one catalogue defeat substring
    matching.
    """

    async def test_the_first_in_stock_result_is_taken(self) -> None:
        mcp = FakeSilpo(
            {
                "кока кола": [
                    product("Напій Coca-Cola", 30.99),
                    product("Мармелад Chupa Chups Cola Tube смак кола", 12.99),
                ]
            }
        )
        cart = await resolve_basket(draft(line("кока кола")), mcp, CONTEXT)
        assert cart.lines[0].name == "Напій Coca-Cola", "cheaper is not more relevant"

    async def test_an_out_of_stock_leader_is_skipped(self) -> None:
        mcp = FakeSilpo(
            {"молоко": [product("Молоко A", 10, stock=0), product("Молоко Б", 90)]},
            {"id-Молоко A": []},
        )
        cart = await resolve_basket(draft(line("молоко")), mcp, CONTEXT)
        assert cart.lines[0].name == "Молоко Б"


class TestResolveWithFallback:
    async def test_a_parenthetical_description_still_resolves(self) -> None:
        """The two «не знайшлося» lines from the live pizza basket."""
        mcp = FakeSilpo({"Ковбаса": [product("Ковбаса «Алан» Салямі Чорізо", 89.90)]})
        cart = await resolve_basket(
            draft(line("Ковбаса (наприклад, салямі або варена)")), mcp, CONTEXT
        )
        assert [ln.name for ln in cart.lines] == ["Ковбаса «Алан» Салямі Чорізо"]
        assert not cart.warnings

    async def test_the_original_description_is_preferred_when_it_matches(self) -> None:
        mcp = FakeSilpo(
            {
                "сир твердий": [product("Сир Пирятин твердий", 649)],
                "сир": [product("Сир плавлений", 30)],
            }
        )
        cart = await resolve_basket(draft(line("сир твердий")), mcp, CONTEXT)
        assert cart.lines[0].name == "Сир Пирятин твердий"
        assert mcp.search_calls == [["сир твердий"]], "no retry when the first search works"

    async def test_a_line_that_matches_nothing_at_all_is_still_reported(self) -> None:
        cart = await resolve_basket(draft(line("Ікра (чорна)")), FakeSilpo({}), CONTEXT)
        assert cart.lines == []
        assert cart.warnings == ["not_found:Ікра (чорна)"]


class TestWeightedQuantities:
    """`price` on a weighted product is per kilogram, so an unqualified «1» orders a
    whole kilo. Live: 2099 ₴ of 36-month Parmigiano in a carbonara basket, at a
    per-kilo price that was itself perfectly fair."""

    def test_an_unqualified_weighted_line_becomes_one_step(self) -> None:
        cheese = product("Пармезан", 2099, stock=5, step=0.1)
        cheese["weighted"] = True
        assert clamp_quantity(1, cheese) == 0.1, "100 g, not a kilogram"

    def test_the_step_is_the_products_own(self) -> None:
        bacon = product("Бекон", 859, stock=5, step=0.25)
        bacon["weighted"] = True
        assert clamp_quantity(1, bacon) == 0.25

    def test_an_explicit_weight_is_honoured(self) -> None:
        """«2 кг картоплі» must stay 2 kg."""
        potatoes = product("Картопля", 30, stock=50, step=0.1)
        potatoes["weighted"] = True
        assert clamp_quantity(2, potatoes) == 2.0

    def test_a_fractional_request_is_untouched(self) -> None:
        cheese = product("Пармезан", 2099, stock=5, step=0.1)
        cheese["weighted"] = True
        assert clamp_quantity(0.2, cheese) == 0.2

    def test_countable_products_are_unaffected(self) -> None:
        """Only weighted goods are priced per kilo; one loaf is one loaf."""
        assert clamp_quantity(1, product("Хліб", 28.50, step=1)) == 1

    async def test_the_cart_shows_a_sane_weight(self) -> None:
        cheese = product("Сир Парміджано Реджано", 2099, stock=5, step=0.1)
        cheese["weighted"] = True
        cart = await resolve_basket(
            draft(line("пармезан")), FakeSilpo({"пармезан": [cheese]}), CONTEXT
        )
        assert cart.lines[0].qty == 0.1
        assert cart.total == Decimal("209.90"), "not 2099.00"
