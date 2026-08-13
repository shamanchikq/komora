"""Matching «прибери ковбаски» to a product in the cart.

A false positive here deletes food from somebody's real shopping. So the interesting
tests are the ones that assert **nothing** matched.
"""

from decimal import Decimal

import pytest

from komora.core.models import ResolvedLine
from komora.core.passes.removals import match_removals, matches, tokens


def line(name: str, description: str = "") -> ResolvedLine:
    return ResolvedLine(
        product_id=f"id-{name}",
        company_id="c",
        branch_id="b",
        name=name,
        description=description,
        qty=1,
        unit="",
        unit_price=Decimal("10"),
        reason_kind="stated",
        reason_text="ви попросили",
    )


SAUSAGE = line("Ковбаски Глобино Салямі Пепероні с/к", "ковбаса пепероні")
CHEESE = line("Сир Яготинська Моцарела міні 45%", "сир моцарела")
FLOUR = line("Борошно пшеничне La Farina di Cuneo для піци", "борошно для піци")


class TestTokens:
    def test_drops_short_words_and_numbers(self) -> None:
        assert tokens("молоко 2,6% для кави") == ["молоко", "кави"]

    def test_case_folds(self) -> None:
        assert tokens("КОВБАСА") == ["ковбаса"]


class TestMatching:
    def test_inflection_is_not_an_obstacle(self) -> None:
        """The user writes «ковбаски», the product says «Ковбаски … Салямі»; a nominative
        rule for the model does not extend to what the user types."""
        assert matches("ковбаски", SAUSAGE)
        assert matches("ковбасу", SAUSAGE)

    def test_matches_the_query_the_line_came_from(self) -> None:
        """«прибери пепероні» — the word is in the description as well as the name."""
        assert matches("пепероні", SAUSAGE)

    def test_matches_a_brand_the_user_can_see_on_screen(self) -> None:
        assert matches("Глобино", SAUSAGE)

    def test_every_word_must_land(self) -> None:
        """«ковбаса варена» must not match a pepperoni sausage just because «ковбаса»
        does — the second word is the whole point of the request."""
        assert not matches("ковбаса варена", SAUSAGE)

    def test_a_shared_prefix_shorter_than_a_stem_is_not_a_match(self) -> None:
        assert not matches("сирок", CHEESE)

    def test_unrelated_words_do_not_match(self) -> None:
        assert not matches("молоко", SAUSAGE)

    def test_a_request_of_only_stopwords_matches_nothing(self) -> None:
        """Otherwise «прибери те, що для…» would empty the cart."""
        assert not matches("для того", SAUSAGE)
        assert not matches("", SAUSAGE)


class TestMatchRemovals:
    def test_resolves_to_the_product_id_the_cart_call_needs(self) -> None:
        found = match_removals(["ковбаски"], [SAUSAGE, CHEESE, FLOUR])
        assert [r.product_id for r in found] == [SAUSAGE.product_id]
        assert found[0].name == SAUSAGE.name

    def test_nothing_matched_removes_nothing(self) -> None:
        assert match_removals(["ікра"], [SAUSAGE, CHEESE]) == []

    def test_every_match_is_returned(self) -> None:
        """«прибери ковбаски» with two in the cart means both."""
        other = line("Ковбаса Столична варена", "ковбаса варена")
        assert len(match_removals(["ковбаса"], [SAUSAGE, other])) == 2

    def test_a_product_the_same_basket_is_adding_is_never_removed(self) -> None:
        """The model naming a product in both `lines` and `removals` must not have
        Komora add it and delete it in one confirmation."""
        assert match_removals(["ковбаски"], [SAUSAGE], keep={SAUSAGE.product_id}) == []

    def test_duplicates_across_baskets_collapse(self) -> None:
        """The same product synced twice is one line in the cart and one removal."""
        assert len(match_removals(["ковбаски"], [SAUSAGE, SAUSAGE])) == 1

    def test_candidates_are_the_only_source(self) -> None:
        """Nothing can be removed if Komora synced nothing — the user's own cart
        contents are not candidates, however well the words fit."""
        assert match_removals(["ковбаски"], []) == []


class TestCommonNounsInflect:
    """A fixed stem length was tuned on «ковбаски»/«ковбаса» and failed on almost every
    other noun a grocery list contains — «прибери колу» matched nothing at all."""

    @pytest.mark.parametrize(
        ("request_text", "product"),
        [
            ("колу", "Напій Coca-Cola Zero кола"),
            ("воду", "Вода Моршинська негазована"),
            ("сиру", "Сир Пирятин твердий 50%"),
            ("яйця", "Яйце куряче С0"),
        ],
    )
    def test_a_declined_noun_still_names_its_product(self, request_text: str, product: str) -> None:
        assert matches(request_text, line(product))

    def test_a_derived_noun_still_does_not(self) -> None:
        """«сирок» is a curd snack. Deleting cheese because the words start alike would
        be a false positive on a real cart."""
        assert not matches("сирок", line("Сир Пирятин твердий 50%"))
