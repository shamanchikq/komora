"""What the user actually reads.

The rules being tested are product rules, not formatting taste: every line carries a
reason, nothing that failed is hidden, and a partial sync never reads as a success.
"""

from decimal import Decimal

import pytest

from komora.bot.bot import MAX_MESSAGE, chunks
from komora.bot.render import (
    esc,
    items,
    money,
    pl,
    quantity,
    render_cart,
    render_sync_preview,
    render_sync_report,
)
from komora.core.models import ResolvedCart, ResolvedLine, SyncReport
from komora.core.sync import SyncPreview


def line(name: str = "Молоко", price: str = "42.90", **kw: object) -> ResolvedLine:
    return ResolvedLine.model_validate(
        {
            "product_id": f"id-{name}",
            "company_id": "c",
            "branch_id": "b",
            "name": name,
            "qty": 1,
            "unit": "900 г",
            "unit_price": Decimal(price),
            "reason_kind": "stated",
            "reason_text": "ви попросили",
        }
        | kw
    )


def cart(*lines: ResolvedLine, **kw: object) -> ResolvedCart:
    total = sum((ln.line_total for ln in lines if not ln.unavailable), Decimal("0"))
    return ResolvedCart.model_validate({"lines": list(lines), "total": total} | kw)


class TestUkrainianPlurals:
    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (1, "позиція"),
            (2, "позиції"),
            (4, "позиції"),
            (5, "позицій"),
            (11, "позицій"),
            (12, "позицій"),
            (14, "позицій"),
            (21, "позиція"),
            (22, "позиції"),
            (25, "позицій"),
            (101, "позиція"),
            (111, "позицій"),
            (0, "позицій"),
        ],
    )
    def test_forms(self, n: int, expected: str) -> None:
        assert pl(n, "позиція", "позиції", "позицій") == expected

    def test_items_reads_naturally(self) -> None:
        assert items(1) == "1 позиція"
        assert items(3) == "3 позиції"


class TestNumbers:
    def test_money_uses_a_comma_and_a_trailing_sign(self) -> None:
        assert money(Decimal("42.9")) == "42,90 ₴"

    def test_money_rounds_half_up(self) -> None:
        assert money(Decimal("0.125")) == "0,13 ₴"

    def test_whole_quantities_stay_whole(self) -> None:
        assert quantity(2.0) == "2"

    def test_weights_keep_their_decimals(self) -> None:
        assert quantity(0.5) == "0,5"


class TestCart:
    def test_every_line_shows_its_reason(self) -> None:
        text = render_cart(cart(line(), line("Хліб", "28.50")), "Кошик")
        assert text.count("— ви попросили") == 2

    def test_line_shows_quantity_price_and_total(self) -> None:
        text = render_cart(cart(line(qty=2)), "Кошик")
        assert "2 × 42,90 ₴ = <b>85,80 ₴</b>" in text

    def test_substitution_names_what_it_replaced(self) -> None:
        text = render_cart(cart(line(substituted_from="Молоко Яготинське")), "Кошик")
        assert "⇄ заміна (було: Молоко Яготинське)" in text

    def test_unavailable_line_stays_visible_and_out_of_the_total(self) -> None:
        text = render_cart(cart(line(), line("Ікра", "900", unavailable=True)), "Кошик")
        assert "Ікра" in text
        assert "не враховано в сумі" in text
        assert "<b>Разом: 42,90 ₴</b>" in text, "the unavailable line is not in the total"

    def test_discount_shows_the_old_price(self) -> None:
        text = render_cart(cart(line(old_price=Decimal("59.90"))), "Кошик")
        assert "було 59,90 ₴" in text

    def test_savings_are_reported_when_they_exist(self) -> None:
        text = render_cart(cart(line(), estimated_savings=Decimal("17.00")), "Кошик")
        assert "Заощаджено ≈ 17,00 ₴" in text

    def test_an_empty_cart_says_so_rather_than_rendering_a_total(self) -> None:
        text = render_cart(cart(warnings=["not_found:щось до чаю"]), "Кошик")
        assert "Нічого не вдалося підібрати" in text
        assert "щось до чаю" in text

    def test_product_names_are_escaped(self) -> None:
        """Telegram HTML: an unescaped `<` in a product name breaks the whole message."""
        text = render_cart(cart(line("Сир <Президент> & Co")), "Кошик")
        assert "&lt;Президент&gt; &amp; Co" in text


class TestWarnings:
    def test_not_found_names_what_was_missing(self) -> None:
        assert "«кава»" in render_cart(cart(line(), warnings=["not_found:кава"]), "К")

    def test_exclusion_names_both_the_item_and_the_restriction(self) -> None:
        text = render_cart(cart(line(), warnings=["excluded:арахісове масло:арахіс"]), "К")
        assert "«арахісове масло»" in text and "(арахіс)" in text

    def test_degraded_coupons_is_admitted(self) -> None:
        assert "Купони зараз недоступні" in render_cart(
            cart(line(), warnings=["degraded:coupons"]), "К"
        )

    def test_an_unrecognised_warning_is_still_shown(self) -> None:
        """A warning nobody translated is worse hidden than ugly."""
        assert "щось_нове:42" in render_cart(cart(line(), warnings=["щось_нове:42"]), "К")

    def test_an_expired_timeslot_says_what_to_do(self) -> None:
        text = render_cart(cart(line(), warnings=["timeslot:expired"]), "К")
        assert "Час доставки" in text and "оберіть новий час" in text


class TestBudget:
    def test_remaining_budget_is_shown(self) -> None:
        text = render_cart(cart(line()), "К", budget_cap=500)
        assert "лишається 457,10 ₴" in text

    def test_overage_offers_the_optional_lines_without_removing_them(self) -> None:
        over = cart(line("Кава", "600"), line("Цукерки", "200", optional=True))
        text = render_cart(over, "К", budget_cap=500)
        assert "перевищено на 300,00 ₴" in text
        assert "200,00 ₴" in text, "what trimming the optional lines would save"
        assert "Це ваш вибір" in text


class TestSyncPreview:
    def test_promises_the_existing_cart_is_untouched(self) -> None:
        preview = SyncPreview(
            existing_count=2,
            existing_total=Decimal("71.40"),
            adding_count=3,
            adding_total=Decimal("164.90"),
        )
        text = render_sync_preview(preview)
        assert "вже 2 позиції" in text and "не чіпаємо" in text
        assert "Додаємо 3 позиції" in text

    def test_overlap_says_replaced_not_added(self) -> None:
        """Quantity is SET, not incremented — promising addition here would be a lie."""
        preview = SyncPreview(
            existing_count=1,
            existing_total=Decimal("42.90"),
            adding_count=1,
            adding_total=Decimal("85.80"),
            overlapping=["Молоко"],
        )
        text = render_sync_preview(preview)
        assert "Молоко" in text and "<b>замінено</b>" in text

    def test_price_drift_shows_both_numbers(self) -> None:
        preview = SyncPreview(
            existing_count=0,
            existing_total=Decimal("0"),
            adding_count=1,
            adding_total=Decimal("52.00"),
            drift=(Decimal("42.90"), Decimal("52.00")),
        )
        text = render_sync_preview(preview)
        assert "було 42,90 ₴" in text and "зараз 52,00 ₴" in text

    def _with(self, *validations: str) -> SyncPreview:
        return SyncPreview(
            existing_count=0,
            existing_total=Decimal("0"),
            adding_count=1,
            adding_total=Decimal("52.00"),
            blocking_validations=list(validations),
        )

    def test_a_validation_code_is_translated(self) -> None:
        """`validations[].message` is a code, not prose. The live run put
        "• product.offer.stock.max" in front of a user."""
        text = render_sync_preview(self._with("product.offer.stock.max"))
        assert "product.offer.stock.max" not in text
        assert "менше, ніж замовлено" in text

    def test_the_timeslot_code_says_what_to_do(self) -> None:
        text = render_sync_preview(self._with("timeslot.not_available"))
        assert "оберіть новий" in text and "застосунку Сільпо" in text

    def test_the_minimum_order_code_says_what_to_do(self) -> None:
        """`order.cost.min` — the code the first real Telegram run produced."""
        text = render_sync_preview(self._with("order.cost.min"))
        assert "order.cost.min" not in text
        assert "додайте ще щось" in text

    def test_an_unknown_code_is_still_shown(self) -> None:
        """A checkout blocker must never be hidden because nobody wrote the Ukrainian
        for it yet."""
        assert "cart.something.new" in render_sync_preview(self._with("cart.something.new"))

    def test_a_repeated_code_is_listed_once(self) -> None:
        """Live: the same code arrived twice, once per offending line."""
        text = render_sync_preview(self._with("product.offer.stock.max", "product.offer.stock.max"))
        assert text.count("менше, ніж замовлено") == 1


class TestSyncReport:
    def test_success_states_how_many_landed(self) -> None:
        text = render_sync_report(SyncReport(ok=True, added=["Молоко", "Хліб"]))
        assert "Готово" in text and "2 позиції" in text

    def test_partial_failure_is_never_dressed_up_as_success(self) -> None:
        report = SyncReport(ok=False, added=["Молоко"], failed=[("Ікра", "немає в наявності")])
        text = render_sync_report(report)
        assert "Готово" not in text
        assert "Додалося не все" in text
        assert "Ікра — немає в наявності" in text

    def test_no_checkout_link_comes_with_the_reason(self) -> None:
        """Live: a cart holding a line that exceeds stock gets no `checkoutWebLink`.
        Reporting success with no link and no reason strands the user."""
        report = SyncReport(
            ok=True,
            added=["Молоко"],
            checkout_web_link=None,
            blocking_validations=["product.offer.stock.max"],
        )
        text = render_sync_report(report)
        assert "Готово" in text
        assert "Оформити поки не вийде" in text
        assert "менше, ніж замовлено" in text

    def test_a_checkout_link_suppresses_the_explanation(self) -> None:
        report = SyncReport(
            ok=True,
            added=["Молоко"],
            checkout_web_link="https://silpo.ua/checkout/abc",
            blocking_validations=["promotion.available"],
        )
        assert "Оформити поки не вийде" not in render_sync_report(report)

    def test_partial_failure_says_a_retry_is_safe(self) -> None:
        """True by construction: quantity is set, not incremented."""
        report = SyncReport(ok=False, added=[], failed=[("Ікра", "помилка")])
        assert "не подвоїть" in render_sync_report(report)


class TestMessageChunking:
    """Lives in the aiogram adapter, but it is a text concern: Telegram rejects
    anything over 4096 characters, and a cart with a reason on every line gets there."""

    def test_short_text_is_one_message(self) -> None:
        assert list(chunks("коротко")) == ["коротко"]

    def test_a_long_cart_is_split_on_line_boundaries(self) -> None:
        text = "\n".join(f"{i}. Молоко Яготинське 2,6%" for i in range(400))
        parts = list(chunks(text))
        assert len(parts) > 1
        assert all(len(p) <= MAX_MESSAGE for p in parts)
        assert "\n".join(parts) == text, "nothing is lost or duplicated"

    def test_a_single_oversized_line_is_not_dropped(self) -> None:
        """Splitting mid-line would break an HTML tag, so an overlong line is sent
        whole and Telegram's own error is the honest outcome."""
        parts = list(chunks("x" * 5000))
        assert parts == ["x" * 5000]


def test_escaping_covers_the_sync_paths_too() -> None:
    report = SyncReport(ok=False, added=[], failed=[("Сир <A&B>", "помилка")])
    assert "&lt;A&amp;B&gt;" in render_sync_report(report)
    assert esc("<x>") == "&lt;x&gt;"


class TestSwapHint:
    """A row of «⇄ N» arrows with no caption reads as decoration."""

    def test_the_hint_appears_with_the_buttons(self) -> None:
        text = render_cart(cart(line(), line("Хліб", "28.50")), "К", swappable=True)
        assert "інший варіант" in text

    def test_no_hint_when_the_buttons_are_absent(self) -> None:
        text = render_cart(cart(line(), line("Хліб", "28.50")), "К")
        assert "інший варіант" not in text

    def test_no_hint_for_a_single_line(self) -> None:
        """One line, one arrow — the numbering explains itself."""
        assert "інший варіант" not in render_cart(cart(line()), "К", swappable=True)
