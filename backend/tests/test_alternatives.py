"""«⇄ N» — the next product for a line the user did not want.

Reported live on 2026-08-12: asked for parmigiano, the swap button offered cheese after
unrelated cheese and never arrived. The category browse was answering on its own
whenever the aisle held two or more products, so «⇄» toured «Тверді і напівтверді сири»
in Silpo's order — a hundred cheeses deep — instead of moving through the parmesans.
"""

import json
import pathlib
from decimal import Decimal
from typing import ClassVar

from komora.core.alternatives import next_alternative
from komora.core.models import ResolvedLine
from komora.core.passes.categories import CategoryIndex
from komora.core.passes.resolve import CATEGORY_PAGE
from tests.fakes import CONTEXT, FakeSilpo, product

INDEX = CategoryIndex(
    json.loads(
        (pathlib.Path(__file__).parent / "fixtures" / "mcp" / "categories.json").read_text(
            encoding="utf-8"
        )
    )
)

HARD_CHEESE = "Тверді і напівтверді сири"

MUZHON = product("Сир «Лавка Традицій» «Мужон витриманий» 30,2%", 775.00)
PARMIGIANO = product("Сир Milkiwita «Пармеджано Реджано» 36% 24 місяці", 299.90)
GRANA = product("Сир «Гранд Падано» твердий 32%", 410.00)
OTHER_CHEESES = [product(f"Сир «Абрикос» {i} 45%", 120.0 + i) for i in range(20)]


def line(current: dict, description: str = "пармезан") -> ResolvedLine:
    return ResolvedLine(
        product_id=str(current["id"]),
        company_id=str(current["companyId"]),
        branch_id=str(current["branchId"]),
        name=str(current["name"]),
        description=description,
        category=HARD_CHEESE,
        qty=0.17,
        unit="",
        unit_price=Decimal(str(current["price"])),
        reason_kind="stated",
        reason_text="для пасти",
    )


class TestTheSwapStaysOnTopicAndOnShelf:
    def _silpo(self) -> FakeSilpo:
        return FakeSilpo(
            {"пармезан": [PARMIGIANO, GRANA]},
            # Silpo's aisle order: the wrong cheese first, the parmesans buried.
            category_products=[MUZHON, *OTHER_CHEESES, PARMIGIANO, GRANA],
        )

    async def test_the_first_swap_reaches_the_relevant_product(self) -> None:
        """Not «Абрикос 0», which is merely the next thing on the shelf."""
        found = await next_alternative(line(MUZHON), self._silpo(), CONTEXT, INDEX)
        assert found is not None
        assert found.name == PARMIGIANO["name"]

    async def test_further_swaps_stay_among_the_relevant_products(self) -> None:
        """The loop the user hit: every tap should move between parmesans, never into
        the rest of the aisle."""
        silpo = self._silpo()
        seen = []
        current = line(MUZHON)
        for _ in range(4):
            current = await next_alternative(current, silpo, CONTEXT, INDEX)
            assert current is not None
            seen.append(current.name)
        assert set(seen) == {PARMIGIANO["name"], GRANA["name"]}, seen

    async def test_it_still_wraps(self) -> None:
        silpo = self._silpo()
        first = await next_alternative(line(MUZHON), silpo, CONTEXT, INDEX)
        second = await next_alternative(first, silpo, CONTEXT, INDEX)
        third = await next_alternative(second, silpo, CONTEXT, INDEX)
        assert third is not None and third.name == first.name

    async def test_the_line_keeps_everything_but_the_product(self) -> None:
        found = await next_alternative(line(MUZHON), self._silpo(), CONTEXT, INDEX)
        assert found is not None
        assert found.description == "пармезан"
        assert found.category == HARD_CHEESE
        assert found.reason_text == "для пасти"
        assert not found.unavailable

    async def test_a_shelf_the_search_cannot_reach_is_still_offered(self) -> None:
        """No search hits at all — the aisle is all there is, and touring it beats
        refusing to swap."""
        silpo = FakeSilpo({}, category_products=[MUZHON, *OTHER_CHEESES])
        found = await next_alternative(line(MUZHON), silpo, CONTEXT, INDEX)
        assert found is not None and found.name != MUZHON["name"]

    async def test_no_category_falls_back_to_the_search(self) -> None:
        silpo = FakeSilpo({"пармезан": [PARMIGIANO, GRANA]})
        current = line(MUZHON).model_copy(update={"category": None})
        found = await next_alternative(current, silpo, CONTEXT, INDEX)
        assert found is not None and found.name == PARMIGIANO["name"]

    async def test_a_single_candidate_offers_nothing(self) -> None:
        silpo = FakeSilpo({"пармезан": [PARMIGIANO]}, category_products=[PARMIGIANO])
        assert await next_alternative(line(PARMIGIANO), silpo, CONTEXT, INDEX) is None

    async def test_the_shelf_is_requested_whole(self) -> None:
        """`narrow` reads a full page as "there may be more", so both callers have to
        ask for the same page size or it decides its fallback on a false premise."""
        silpo = self._silpo()
        await next_alternative(line(MUZHON), silpo, CONTEXT, INDEX)
        assert silpo.category_limits == [CATEGORY_PAGE]


class TestAWrongCategoryIsEscapable:
    """The trap, from the live run of 2026-08-12.

    The model named a plausible but wrong category — the artisan cheeses, not the aisle
    holding parmesan. The intersection came out empty, the shelf won outright, and «⇄»
    cycled three craft cheeses forever while thirty genuine Parmigiano Reggianos sat in
    the search results with no way to reach them. Probed against live Silpo: the search
    for «пармезан» is excellent, so discarding it was the whole mistake.
    """

    CRAFT: ClassVar[list[dict]] = [
        product("Сир Лавка Традицій Чізарня Качокавалло", 839.00),
        product("Сир Лавка Традицій Будз Баран Драй Джек крафт", 1049.00),
        product("Сир Лавка традицій Плай Карпатський твердий", 999.00),
    ]
    PARMIGIANO = product("Сир Ghidetti «Парміджано Реджано» 44%", 369.00)
    GRANA = product("Сир Ghidetti «Грана Падано» тертий 42%", 169.00)

    def _silpo(self) -> FakeSilpo:
        # A small, complete shelf that shares nothing with the search.
        return FakeSilpo({"пармезан": [self.PARMIGIANO, self.GRANA]}, category_products=self.CRAFT)

    async def test_one_tap_reaches_the_search_result(self) -> None:
        current = line(self.CRAFT[0]).model_copy(update={"category": "Крафтові сири"})
        found = await next_alternative(current, self._silpo(), CONTEXT, INDEX)
        assert found is not None
        assert found.name == self.PARMIGIANO["name"], "a wrongly named category must not be a cage"

    async def test_the_search_results_are_all_reachable(self) -> None:
        silpo = self._silpo()
        current = line(self.CRAFT[0]).model_copy(update={"category": "Крафтові сири"})
        seen = []
        for _ in range(5):
            current = await next_alternative(current, silpo, CONTEXT, INDEX)
            assert current is not None
            seen.append(current.name)
        assert self.PARMIGIANO["name"] in seen and self.GRANA["name"] in seen, seen

    async def test_the_shelf_still_leads_when_it_is_complete(self) -> None:
        """Resolution keeps the protection the category was added for; only the swap
        list gained an escape."""
        from komora.core.passes.resolve import narrow

        ordered = narrow([self.PARMIGIANO], self.CRAFT)
        assert ordered[0]["name"] == self.CRAFT[0]["name"]
        assert ordered[1]["name"] == self.PARMIGIANO["name"], "escape is one step away"
