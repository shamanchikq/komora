"""Plan 4 Task 0: the fixtures re-captured live on 2026-09-14, read by the code that uses
them (`scripts/capture_task0.py`; reference §10).

Structure only, never a count the afternoon decided: nine promotions and six coupons
were one branch and one account at one hour. The sanitised receipt and the tool list
are the two places a count *is* the point — one receipt was kept on purpose, and a tool
the model is given must be annotated.
"""

import json
import pathlib
from decimal import Decimal
from typing import Any

from komora.core.agent.tools import (
    READ_TOOLS,
    UNUSED_HEADINGS,
    build_tool_decls,
)
from komora.core.habits.purchases import receipt_totals
from komora.core.passes.promos import coupon_usable, describe_coupons
from komora.core.passes.resolve import special_prices_of
from komora.core.units import parse_display_ratio

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "mcp"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


TOOLS = fixture("tools")
BY_NAME = {t["name"]: t for t in TOOLS}
DECLS = {d.name: d for d in build_tool_decls(TOOLS)}


def products(name: str) -> list[dict[str, Any]]:
    payload = fixture(name)
    if "queries" in payload:
        return [p for q in payload["queries"] for p in q["products"]]
    rows: list[dict[str, Any]] = payload["products"]
    return rows


class TestToolList:
    def test_every_tool_is_annotated(self) -> None:
        assert all(t.get("annotations") for t in TOOLS)

    def test_the_cart_creating_write_arrived_and_stays_unreachable(self) -> None:
        create = BY_NAME["silpo_create_shopping_cart"]
        assert create["annotations"]["readOnlyHint"] is False
        assert "silpo_create_shopping_cart" not in READ_TOOLS

    def test_similar_products_requires_the_whole_slot(self) -> None:
        required = set(BY_NAME["silpo_get_similar_products"]["inputSchema"]["required"])
        assert {"deliveryType", "timeslotStart", "timeslotEnd"} <= required

    def test_online_orders_declare_the_ceiling_measured_live(self) -> None:
        limit = BY_NAME["silpo_get_my_online_orders"]["inputSchema"]["properties"]["limit"]
        assert limit["maximum"] == 50


class TestWhatTheModelReadsOfTheLiveDescriptions:
    def test_search_keeps_article_package_and_weighted_units(self) -> None:
        text = DECLS["silpo_find_products_batch"].description
        assert "SEARCH BY ARTICLE CODE" in text
        assert "PACKAGE SIZE" in text
        assert "KILOGRAMS" in text, "the weighted-units sentence names a write tool in an aside"

    def test_an_aside_naming_an_unreachable_tool_goes_the_sentence_stays(self) -> None:
        text = DECLS["silpo_find_products_batch"].description
        assert "silpo_add_or_update_cart_products" not in text
        assert "silpo_get_my_offline_orders" not in text

    def test_get_products_pointers_resolve_to_paragraphs_the_model_has(self) -> None:
        """`inStock`, `sortBy`, `sortDirection`, `fromPrice`, `toPrice` say "see tool
        description". With SORT ORDER and PRICE FILTERS kept, that is true — so the
        pointers need no inlining, and this fails the day they stop being true."""
        decl = DECLS["silpo_get_products"]
        pointing = [
            name
            for name, prop in decl.parameters["properties"].items()
            if "tool description" in (prop.get("description") or "").lower()
        ]
        assert pointing
        assert "SORT ORDER" in decl.description and "PRICE FILTERS" in decl.description
        assert "PACKAGE SIZE" in decl.description

    def test_no_unused_heading_reaches_the_model(self) -> None:
        for decl in DECLS.values():
            assert not any(f"{heading}:" in decl.description for heading in UNUSED_HEADINGS)


class TestDeals:
    def test_promotions_carry_codes_and_no_amounts(self) -> None:
        payload = fixture("promotions")
        assert sorted(payload) == ["promotions", "success", "summary"]
        for promotion in payload["promotions"]:
            assert set(promotion) == {"code", "title", "productCount", "url"}

    def test_a_promotion_is_membership_not_a_lower_price(self) -> None:
        """«Пакунок школяра», captured: two notebooks at 29,99 instead of 39,99 and three
        pens and tape with no `oldPrice` and no `specialPrices` at all. Being in a
        promotion is not a deal — D2's `oldPrice > price` is the only rule that says one."""
        rows = products("products_by_promotion")
        assert any(p["oldPrice"] is not None and p["oldPrice"] > p["price"] for p in rows)
        assert any(p["oldPrice"] is None and not p.get("specialPrices") for p in rows)

    def test_every_discounted_product_has_an_old_price_above_its_price(self) -> None:
        for p in products("products_on_promotion"):
            assert p["oldPrice"] > p["price"], p["name"]

    def test_every_captured_display_ratio_parses(self) -> None:
        """Task 0 measured 300 discounted products: no `null`, and 13 multipack or bare
        «шт» forms the parser did not read until it was taught them."""
        for name in (
            "products_on_promotion",
            "products_by_promotion",
            "find_products_batch_display",
        ):
            for p in products(name):
                assert parse_display_ratio(p["displayRatio"]) is not None, p["displayRatio"]

    def test_unit_goods_price_per_unit_and_weighted_goods_per_display_ratio(self) -> None:
        for p in products("products_on_promotion"):
            if p["weighted"]:
                assert p["displayRatio"] == "100г"
            else:
                assert p["displayPrice"] == p["price"]

    def test_special_prices_are_from_type_and_read_into_the_model(self) -> None:
        hits = [p for p in products("find_products_batch_display") if p.get("specialPrices")]
        assert hits, "the capture searched a multi-buy product's article"
        for p in hits:
            assert {s["type"] for s in p["specialPrices"]} == {"from"}
            parsed = special_prices_of(p)
            assert parsed and all(
                s.count >= 2 and s.price < Decimal(str(p["price"])) for s in parsed
            )

    def test_sets_are_slugs_with_titles(self) -> None:
        for s in fixture("product_sets")["sets"]:
            assert s["slug"] and s["title"]


class TestCouponsAndPromos:
    def test_the_list_carries_value_fields(self) -> None:
        for coupon in fixture("my_coupons")["coupons"]:
            assert {"rewardText", "rewardValue", "rewardUnit", "rewardSign", "endDateTime"} <= set(
                coupon
            )

    def test_details_name_applicability_and_progress(self) -> None:
        coupon = fixture("coupon_details")["coupon"]
        assert "canBeAppliedToOrder" in coupon and "progress" in coupon

    def test_state_is_not_eligibility(self) -> None:
        """The captured coupon reads «Активний» and is neither active nor applicable."""
        coupon = fixture("coupon_details")["coupon"]
        assert coupon["state"] == "Активний" and not coupon_usable(coupon)

    def test_only_usable_coupons_are_described_with_their_reward(self) -> None:
        coupons = fixture("my_coupons")["coupons"]
        notes = describe_coupons(coupons)
        assert len(notes) == sum(1 for c in coupons if coupon_usable(c))
        assert all(any(c["rewardText"] in n for c in coupons) for n in notes)

    def test_personal_promos_are_selectable_only_in_silpo(self) -> None:
        payload = fixture("my_promos")
        assert {"minSelect", "maxSelect"} <= set(payload["meta"])
        assert all("selected" in p and "rewardText" in p for p in payload["promos"])


class TestReceiptWithRewards:
    def test_totals_are_read_from_the_receipt(self) -> None:
        [totals] = receipt_totals(fixture("my_offline_order_rewards"))
        assert totals.total > 0 and totals.discount > 0

    def test_rewards_carry_a_group_and_a_promo_id_key(self) -> None:
        [receipt] = fixture("my_offline_order_rewards")["orders"]
        assert receipt["rewards"]
        for reward in receipt["rewards"]:
            assert reward["rewardGroupCodeName"].startswith("CN_REWARD_")
            assert "promoId" in reward

    def test_the_receipt_link_is_a_placeholder(self) -> None:
        """A real `receiptUrl` opens that shopper's receipt. It never reaches a fixture."""
        [receipt] = fixture("my_offline_order_rewards")["orders"]
        assert receipt["receiptUrl"].endswith("/FIXTURE0002")
