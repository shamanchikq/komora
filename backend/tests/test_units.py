"""Pack sizes: `displayRatio` parsed, a stated need turned into packs (Plan 4 D8).

Every accepted form here was observed live on 2026-09-14 (reference §10.3); the
rejected ones are the shapes that *look* like sizes and are not one pack's content.
"""

import pytest

from komora.core.models import Amount, DraftBasket, DraftLine
from komora.core.passes.resolve import quantity_for, resolve_basket
from komora.core.units import (
    PackSize,
    kilograms_for,
    packs_for,
    parse_display_ratio,
    parse_need,
)
from tests.fakes import CONTEXT, FakeSilpo, product


class TestParseDisplayRatio:
    @pytest.mark.parametrize(
        ("raw", "amount", "unit"),
        [
            ("900г", 900, "g"),
            ("1000г", 1000, "g"),
            ("150г", 150, "g"),
            ("0,5л", 500, "ml"),
            ("3л", 3000, "ml"),
            ("36г", 36, "g"),
            ("10 шт", 10, "pcs"),
            ("100г", 100, "g"),
            ("1.5кг", 1500, "g"),
            ("250 мл", 250, "ml"),
        ],
    )
    def test_observed_forms(self, raw: str, amount: float, unit: str) -> None:
        assert parse_display_ratio(raw) == PackSize(amount=amount, unit=unit)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "raw", [None, "", "<=0,5", "2*100г", "г", "0г", "big", 900, "900 units"]
    )
    def test_anything_else_is_none_never_a_guess(self, raw: object) -> None:
        assert parse_display_ratio(raw) is None


class TestPacks:
    def test_a_need_is_rounded_up_to_whole_packs(self) -> None:
        need = parse_need(1.2, "кг")
        assert need is not None
        assert packs_for(need, PackSize(1000, "g")) == 2

    def test_an_exact_fit_is_not_rounded_up(self) -> None:
        need = parse_need(1.8, "кг")
        assert need is not None
        assert packs_for(need, PackSize(900, "g")) == 2

    def test_grams_and_millilitres_are_never_converted(self) -> None:
        need = parse_need(1, "л")
        assert need is not None
        assert packs_for(need, PackSize(900, "g")) is None

    def test_a_weighted_need_is_kilograms(self) -> None:
        need = parse_need(300, "г")
        assert need is not None
        assert kilograms_for(need) == 0.3
        assert kilograms_for(PackSize(500, "ml")) is None

    def test_an_unknown_unit_or_a_bad_number_is_none(self) -> None:
        assert parse_need(1, "пачки") is None
        assert parse_need(0, "кг") is None
        assert parse_need(float("nan"), "кг") is None


class TestQuantityFor:
    """`resolve` turns the need into packs only when both halves are known."""

    def test_packs_from_the_pack_size(self) -> None:
        bag = product("Картопля 1 кг", 30, display_ratio="1кг", stock=20)
        assert quantity_for(1, Amount(value=2.5, unit="кг"), bag) == 3

    def test_no_pack_size_keeps_the_models_number(self) -> None:
        august = product("Картопля", 30)  # the fixture's shape: no displayRatio
        assert quantity_for(2, Amount(value=2.5, unit="кг"), august) == 2

    def test_a_weighted_good_takes_the_need_in_kilograms(self) -> None:
        cheese = product("Сир", 400, weighted=True, step=0.1, display_ratio="100г")
        assert quantity_for(1, Amount(value=300, unit="г"), cheese) == 0.3

    def test_no_amount_is_the_old_path_exactly(self) -> None:
        cheese = product("Сир", 400, weighted=True, step=0.1)
        assert quantity_for(1, None, cheese) == 0.1  # unqualified weighted → one step

    def test_the_stock_ceiling_still_applies(self) -> None:
        bag = product("Картопля 1 кг", 30, display_ratio="1кг", stock=2)
        assert quantity_for(1, Amount(value=5, unit="кг"), bag) == 2

    async def test_through_resolve(self) -> None:
        silpo = FakeSilpo({"молоко": [product("Молоко", 40, display_ratio="0,5л", stock=50)]})
        basket = DraftBasket(
            title="План",
            intent="mealplan",
            lines=[
                DraftLine(
                    description="молоко",
                    amount=Amount(value=3, unit="л"),
                    reason_text="для каші",
                )
            ],
        )
        cart = await resolve_basket(basket, silpo, CONTEXT)
        assert cart.lines[0].qty == 6
        assert cart.lines[0].display_ratio == "0,5л"
