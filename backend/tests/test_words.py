"""The one rule two passes were each getting wrong in their own way.

Both tables below are measured cases from the real category tree and real removal
requests, not invented ones.
"""

import pytest

from komora.core.passes.words import same_word


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("кола", "кола"),
        ("колу", "кола"),
        ("воду", "вода"),
        ("сиру", "сир"),
        ("яйця", "яйце"),
        ("ковбаски", "ковбаса"),
        ("тверді", "твердий"),
        ("молока", "молоко"),
        ("сири", "сир"),
    ],
)
def test_an_ending_is_not_a_different_word(first: str, second: str) -> None:
    assert same_word(first, second)
    assert same_word(second, first), "the rule must be symmetric"


@pytest.mark.parametrize(
    ("first", "second", "why"),
    [
        ("кола", "колаген", "the bug: a correct category answer got a collagen shelf"),
        ("вино", "виноград", "and a grape one"),
        ("сирок", "сир", "a curd snack is not cheese"),
        ("сир", "сирний", "nor is a cheese-flavoured thing"),
        ("сік", "сіль", "different words that start alike"),
        ("молоко", "молочний", "an adjective is not the noun"),
    ],
)
def test_a_longer_suffix_changes_the_word(first: str, second: str, why: str) -> None:
    assert not same_word(first, second), why
    assert not same_word(second, first), why


def test_short_words_must_match_outright() -> None:
    """Two shared letters agree with far too much to mean anything."""
    assert not same_word("ік", "іх")
    assert same_word("сік", "сік")


def test_empty_matches_nothing() -> None:
    assert not same_word("", "кола")
    assert not same_word("кола", "")
    assert same_word("", "")
