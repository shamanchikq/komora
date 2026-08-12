"""Silpo's taxonomy, matched against the real captured tree.

Free-text search conflates «Курячі яйця» with «Яйця цесарки»; the catalogue keeps them
in separate categories, and browsing one is the only way to say which was meant.
"""

import json
import pathlib

import pytest

from komora.core.models import DraftBasket, DraftLine
from komora.core.passes.categories import MAX_PAGES, CategoryIndex, fetch_categories
from komora.core.passes.resolve import CATEGORY_PAGE, resolve_basket
from tests.fakes import CONTEXT, FakeSilpo, product

LIVE_TREE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "mcp" / "categories.json").read_text(
        encoding="utf-8"
    )
)
INDEX = CategoryIndex(LIVE_TREE)


class TestAgainstTheRealTree:
    def test_the_capture_is_the_whole_tree(self) -> None:
        assert len(INDEX) > 500, "a partial tree would silently stop matching"

    def test_an_exact_title_matches(self) -> None:
        assert INDEX.slug_for("Курячі яйця") == "kuriachi-iaitsia-4977"

    def test_matching_ignores_case(self) -> None:
        assert INDEX.slug_for("курячі яйця") == "kuriachi-iaitsia-4977"

    def test_the_distinctions_search_cannot_make_are_all_there(self) -> None:
        """The reason this exists at all."""
        assert INDEX.slug_for("Перепелині яйця") != INDEX.slug_for("Курячі яйця")
        assert INDEX.slug_for("Фермерські яйця") is not None

    def test_a_looser_phrase_finds_the_shortest_containing_title(self) -> None:
        """«яйця» alone should land on the general category, not a niche one."""
        assert INDEX.slug_for("яйця") == "yaitsia-528"

    @pytest.mark.parametrize("unknown", ["", None, "   ", "марсіанські делікатеси"])
    def test_no_match_falls_back_to_search(self, unknown: str | None) -> None:
        """A wrong category is worse than none: it narrows the catalogue to the wrong
        shelf, and nothing in the product name gives that away."""
        assert INDEX.slug_for(unknown) is None

    def test_a_stop_word_alone_matches_nothing(self) -> None:
        assert INDEX.slug_for("та") is None


CATALOGUE = {"яйця": [product("Яйця цесарки Пташина Династія", 257.40)]}
IN_CATEGORY = [product("Яйця курячі С1 «Квочка»", 59.39)]


class TestResolveUsesCategories:
    def _basket(self, category: str | None) -> DraftBasket:
        return DraftBasket(
            title="Кошик",
            intent="stated",
            lines=[DraftLine(description="яйця", category=category, reason_text="ви попросили")],
        )

    async def test_a_named_category_beats_the_search_result(self) -> None:
        mcp = FakeSilpo(CATALOGUE, category_products=IN_CATEGORY)
        cart = await resolve_basket(self._basket("Курячі яйця"), mcp, CONTEXT, INDEX)
        assert cart.lines[0].name == "Яйця курячі С1 «Квочка»"
        assert mcp.category_calls == ["kuriachi-iaitsia-4977"]

    async def test_no_category_falls_back_to_search(self) -> None:
        mcp = FakeSilpo(CATALOGUE, category_products=IN_CATEGORY)
        cart = await resolve_basket(self._basket(None), mcp, CONTEXT, INDEX)
        assert cart.lines[0].name == "Яйця цесарки Пташина Династія"
        assert mcp.category_calls == []

    async def test_an_unrecognised_category_falls_back_to_search(self) -> None:
        mcp = FakeSilpo(CATALOGUE, category_products=IN_CATEGORY)
        cart = await resolve_basket(self._basket("вигадана категорія"), mcp, CONTEXT, INDEX)
        assert cart.lines[0].name == "Яйця цесарки Пташина Династія"

    async def test_an_empty_category_falls_back_to_search(self) -> None:
        """A category that exists but holds nothing here must not lose the line."""
        mcp = FakeSilpo(CATALOGUE, category_products=[])
        cart = await resolve_basket(self._basket("Курячі яйця"), mcp, CONTEXT, INDEX)
        assert cart.lines[0].name == "Яйця цесарки Пташина Династія"

    async def test_a_failing_category_call_falls_back_to_search(self) -> None:
        mcp = FakeSilpo(CATALOGUE, fails={"get_products"})
        cart = await resolve_basket(self._basket("Курячі яйця"), mcp, CONTEXT, INDEX)
        assert cart.lines[0].name == "Яйця цесарки Пташина Династія"

    async def test_one_call_per_distinct_category(self) -> None:
        basket = DraftBasket(
            title="Кошик",
            intent="stated",
            lines=[
                DraftLine(description="яйця", category="Курячі яйця", reason_text="r"),
                DraftLine(description="ще яйця", category="Курячі яйця", reason_text="r"),
            ],
        )
        mcp = FakeSilpo(CATALOGUE, category_products=IN_CATEGORY)
        await resolve_basket(basket, mcp, CONTEXT, INDEX)
        assert mcp.category_calls == ["kuriachi-iaitsia-4977"], "deduplicated"


class TestTheCaptureIsComplete:
    """`limit` caps at 1000 and the tree is 1010 rows. The first capture stopped at
    1000 — a suspiciously round number that reads as "all of them" — and dropped ten
    **top-level** categories, orphaning 71 of their children."""

    def test_every_row_the_server_has(self) -> None:
        assert len(LIVE_TREE["categories"]) == LIVE_TREE["meta"]["total"]

    def test_no_category_points_at_a_parent_that_is_missing(self) -> None:
        ids = {c["id"] for c in LIVE_TREE["categories"]}
        orphans = [
            c["title"]
            for c in LIVE_TREE["categories"]
            if c.get("parentId") and c["parentId"] not in ids
        ]
        assert orphans == []

    @pytest.mark.parametrize("title", ["Вода", "Побутова хімія", "Особиста гігієна та здоров'я"])
    def test_the_categories_the_truncation_lost(self, title: str) -> None:
        assert INDEX.slug_for(title) is not None


class TestPagination:
    async def test_it_follows_meta_total_past_the_page_limit(self) -> None:
        tree = [{"id": str(i), "title": f"К{i}", "slug": f"k-{i}"} for i in range(1010)]
        mcp = FakeSilpo(categories=tree)
        found = await fetch_categories(mcp, CONTEXT)
        assert len(found) == 1010
        assert mcp.category_pages == [(0, 1000), (1000, 1000)]

    async def test_a_single_page_costs_one_call(self) -> None:
        tree = [{"id": "1", "title": "К", "slug": "k"}]
        mcp = FakeSilpo(categories=tree)
        assert len(await fetch_categories(mcp, CONTEXT)) == 1
        assert len(mcp.category_pages) == 1

    async def test_a_server_that_never_advances_cannot_loop_forever(self) -> None:
        """`meta.total` is the server's claim; the loop must not trust it blindly."""
        tree = [{"id": "1", "title": "К", "slug": "k"}]
        mcp = FakeSilpo(categories=tree)

        async def stuck(context, **filters):  # type: ignore[no-untyped-def]
            mcp.category_pages.append((filters.get("offset", 0), filters.get("limit", 0)))
            return {"success": True, "categories": tree, "meta": {"total": 99999}}

        mcp.get_categories = stuck  # type: ignore[method-assign]
        await fetch_categories(mcp, CONTEXT)
        assert len(mcp.category_pages) == MAX_PAGES


class TestTheShelfNarrowsTheSearch:
    """A category says which aisle, not which product.

    `get_products` has no query parameter — it returns the category in Silpo's own
    order — so reading the first item off it ignored the description entirely. Live on
    2026-08-12: asked «замiни сир мухон на пармезан», Komora proposed the very cheese it
    had been asked to replace, three turns running, because that cheese sits at the top
    of the hard-cheese aisle.
    """

    HARD_CHEESE = "Тверді і напівтверді сири"
    MUZHON = product("Сир «Лавка Традицій» «Мужон витриманий» 30,2%", 775.00)
    PARMESAN = product("Сир Milkiwita «Пармеджано Реджано» 36% 24 місяці", 299.90)

    def _basket(self, category: str | None = None) -> DraftBasket:
        return DraftBasket(
            title="Паста карбонара",
            intent="stated",
            lines=[DraftLine(description="пармезан", category=category, reason_text="для пасти")],
        )

    async def test_the_search_decides_which_product_on_the_named_shelf(self) -> None:
        mcp = FakeSilpo(
            {"пармезан": [self.PARMESAN]},
            # Silpo's order, with the wrong cheese first — this is the shelf as returned.
            category_products=[self.MUZHON, self.PARMESAN],
        )
        cart = await resolve_basket(self._basket(self.HARD_CHEESE), mcp, CONTEXT, INDEX)
        assert cart.lines[0].name == self.PARMESAN["name"], (
            "the first item on the shelf is not the answer to the question asked"
        )

    async def test_a_shelf_seen_in_full_still_overrides_an_off_aisle_search(self) -> None:
        """The protection categories were introduced for: «основа для піци» matching an
        ice-cream cone. Nothing the search found is on the shelf, and the shelf is short
        enough that we know we saw all of it."""
        cone = product("Корзинка Progelcone для морозива цукрова", 39.90)
        base = product("Основа для піци Мlivita", 54.90)
        mcp = FakeSilpo({"пармезан": [cone]}, category_products=[base])
        cart = await resolve_basket(self._basket(self.HARD_CHEESE), mcp, CONTEXT, INDEX)
        assert cart.lines[0].name == base["name"]

    async def test_a_truncated_shelf_defers_to_the_search(self) -> None:
        """A full page means the match may simply be further down the aisle, so an
        unmatched intersection is not evidence the search was wrong."""
        filler = [product(f"Сир інший {i}", 100.0 + i) for i in range(CATEGORY_PAGE)]
        mcp = FakeSilpo({"пармезан": [self.PARMESAN]}, category_products=filler)
        cart = await resolve_basket(self._basket(self.HARD_CHEESE), mcp, CONTEXT, INDEX)
        assert cart.lines[0].name == self.PARMESAN["name"]

    async def test_the_whole_shelf_is_requested(self) -> None:
        """A shelf cut short cannot be told from a shelf the product is not on."""
        mcp = FakeSilpo({"пармезан": [self.PARMESAN]}, category_products=[self.MUZHON])
        await resolve_basket(self._basket(self.HARD_CHEESE), mcp, CONTEXT, INDEX)
        assert mcp.category_limits == [CATEGORY_PAGE]
