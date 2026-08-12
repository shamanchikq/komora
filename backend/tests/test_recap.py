"""What the model is told about its own last turn.

The recap exists because history held only a basket's title. Asked next turn to change
one line, the model rebuilt the whole basket from the intent and silently altered lines
nobody had mentioned.
"""

from decimal import Decimal

from komora.core.agent.recap import MAX_ITEMS, SYNCED_TAG, draft_recap, sync_recap
from komora.core.models import ResolvedCart, ResolvedLine, SyncReport


def line(name: str, description: str, qty: float = 1, **kw: object) -> ResolvedLine:
    return ResolvedLine.model_validate(
        {
            "product_id": f"id-{name}",
            "company_id": "c",
            "branch_id": "b",
            "name": name,
            "description": description,
            "qty": qty,
            "unit": "",
            "unit_price": Decimal("10"),
            "reason_kind": "stated",
            "reason_text": "ви попросили",
        }
        | kw
    )


PIZZA = ResolvedCart(
    lines=[
        line("Борошно La Farina di Cuneo для піци", "борошно для піци"),
        line("Ковбаски Глобино Салямі Пепероні с/к", "ковбаса пепероні"),
    ],
    total=Decimal("20"),
)


class TestDraftRecap:
    def test_records_the_products_not_just_the_title(self) -> None:
        """The bug this exists for: «[чернетка] Інгредієнти для піци» alone told the
        model nothing about what to edit."""
        text = draft_recap("Інгредієнти для піци пепероні", PIZZA)
        assert "Ковбаски Глобино Салямі Пепероні с/к" in text
        assert "Борошно La Farina di Cuneo для піци" in text

    def test_keeps_the_query_alongside_the_product(self) -> None:
        """The description is what an unchanged line must be re-proposed as; the name
        is what the user is looking at and will refer to."""
        text = draft_recap("Піца", PIZZA)
        assert "ковбаса пепероні" in text and "Ковбаски Глобино" in text

    def test_quantities_survive(self) -> None:
        cart = ResolvedCart(lines=[line("Яйця", "яйця курячі", qty=2)], total=Decimal("20"))
        assert "× 2" in draft_recap("Сніданок", cart)

    def test_a_weighted_quantity_is_not_padded(self) -> None:
        cart = ResolvedCart(lines=[line("Пармезан", "пармезан", qty=0.1)], total=Decimal("1"))
        assert "× 0.1" in draft_recap("Паста", cart)

    def test_an_unavailable_line_is_marked(self) -> None:
        """It was never sent, so it must not read as something in the cart."""
        cart = ResolvedCart(lines=[line("Ікра", "ікра", unavailable=True)])
        assert "немає в наявності" in draft_recap("Свято", cart)

    def test_a_long_basket_is_bounded(self) -> None:
        cart = ResolvedCart(lines=[line(f"Товар {i}", f"товар {i}") for i in range(40)])
        text = draft_recap("Велике", cart)
        assert f"…ще {40 - MAX_ITEMS}" in text
        assert len(text.splitlines()) <= MAX_ITEMS + 2


class TestSyncRecap:
    def test_says_the_products_are_in_the_real_cart(self) -> None:
        """The tag the prompt keys on: seeing it is how the model knows an edit needs
        `removals` rather than another line."""
        text = sync_recap(SyncReport(ok=True, added=["Ковбаски Глобино"]))
        assert text.startswith(SYNCED_TAG)
        assert "Ковбаски Глобино" in text

    def test_a_removal_is_recorded(self) -> None:
        text = sync_recap(SyncReport(ok=True, removed=["Ковбаски Глобино"]))
        assert "прибрано" in text and "Ковбаски Глобино" in text

    def test_a_partial_sync_is_not_recorded_as_a_whole_one(self) -> None:
        """Telling the model a line landed when it did not would have it propose a
        removal for something that was never there."""
        text = sync_recap(SyncReport(ok=False, added=["Молоко"], failed=[("Хліб", "немає")]))
        assert "Молоко" in text and "не додалося" in text and "Хліб" in text

    def test_nothing_happening_is_said_plainly(self) -> None:
        assert "нічого не змінилося" in sync_recap(SyncReport(ok=True))
