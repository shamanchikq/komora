"""Domain models: the two representations every intent flows through.

DraftBasket is what the user *wants* (descriptions, no SKUs); ResolvedCart is what
the deterministic passes turned it into (real SKUs, prices, substitutions).
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from komora.core.models import DraftBasket, DraftLine, ResolvedCart, ResolvedLine, SyncReport


def line(**overrides: object) -> ResolvedLine:
    base = {
        "product_id": "p1",
        "company_id": "c1",
        "branch_id": "b1",
        "name": "Молоко Яготинське 2,6%",
        "qty": 2,
        "unit": "900 мл",
        "unit_price": Decimal("42.90"),
        "reason_kind": "habit",
        "reason_text": "купуєте кожні ~4 дні",
    }
    return ResolvedLine.model_validate(base | overrides)


class TestDraftBasket:
    def test_parses_the_shape_an_llm_emits(self) -> None:
        """This mirrors a propose_basket tool call payload."""
        basket = DraftBasket.model_validate(
            {
                "title": "Звичайний кошик",
                "intent": "stated",
                "lines": [
                    {"description": "молоко 2,6%", "quantity": 2, "reason_text": "ви попросили"},
                    {"description": "хліб", "reason_text": "ви попросили"},
                ],
            }
        )
        assert len(basket.lines) == 2
        assert basket.lines[1].quantity == 1, "quantity should default to 1"
        assert basket.lines[0].reason_kind == "stated"
        assert basket.lines[0].optional is False

    def test_reason_text_is_required(self) -> None:
        """No line may reach a user without a reason — spec G2."""
        with pytest.raises(ValidationError, match="reason_text"):
            DraftLine.model_validate({"description": "молоко"})

    def test_unknown_reason_kind_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DraftLine.model_validate(
                {"description": "x", "reason_text": "y", "reason_kind": "invented"}
            )


class TestResolvedCart:
    def test_defaults_to_an_empty_cart(self) -> None:
        cart = ResolvedCart(lines=[])
        assert cart.total == Decimal("0")
        assert cart.estimated_savings == Decimal("0")
        assert cart.savings_notes == [] and cart.warnings == []

    def test_mutable_defaults_are_not_shared_between_instances(self) -> None:
        """A classic Pydantic footgun — one cart's warnings must not leak into another's."""
        first, second = ResolvedCart(lines=[]), ResolvedCart(lines=[])
        first.warnings.append("degraded:coupons")
        assert second.warnings == []

    def test_prices_stay_decimal_not_float(self) -> None:
        """Money in float silently drifts; totals must be exact."""
        cart = ResolvedCart(lines=[line(), line(unit_price=Decimal("0.10"))])
        assert isinstance(cart.lines[0].unit_price, Decimal)
        assert sum((ln.line_total for ln in cart.lines), Decimal("0")) == Decimal("86.00")

    def test_line_total_of_a_fractional_quantity_is_exact(self) -> None:
        """1.2 kg at 54,90 ₴ is exactly 65,88 ₴ — not 65.87999999999999."""
        assert line(qty=1.2, unit_price=Decimal("54.90")).line_total == Decimal("65.88")

    def test_multiplying_price_by_qty_directly_is_a_type_error(self) -> None:
        """Guards the reason line_total exists: the naive expression does not work."""
        ln = line()
        with pytest.raises(TypeError):
            _ = ln.unit_price * ln.qty  # type: ignore[operator]

    def test_substitution_and_unavailability_are_representable(self) -> None:
        substituted = line(substituted_from="Йогурт «Активіа»", reason_kind="sub")
        missing = line(unavailable=True)
        assert substituted.substituted_from == "Йогурт «Активіа»"
        assert missing.unavailable is True


class TestSyncReport:
    def test_partial_failure_is_representable_and_not_ok(self) -> None:
        report = SyncReport(ok=False, added=["Молоко"], failed=[("Хліб", "out of stock")])
        assert report.ok is False
        assert report.failed[0] == ("Хліб", "out of stock")
        assert report.checkout_web_link is None

    def test_serialises_round_trip(self) -> None:
        report = SyncReport(ok=True, added=["Молоко"], failed=[], checkout_web_link="https://s/co")
        assert SyncReport.model_validate_json(report.model_dump_json()) == report


class TestModelProse:
    """Everything the model writes is shown to a person, so it is cleaned here rather
    than at each render site."""

    def test_stray_markup_is_stripped_from_a_title(self) -> None:
        """Live, first Telegram run: gemma4:12b titled a basket «Базові продукти</div>».
        Escaping kept the message intact, so the user just read a stray `</div>`."""
        basket = DraftBasket(
            title="Базові продукти</div>",
            intent="stated",
            lines=[DraftLine(description="молоко", reason_text="ви попросили")],
        )
        assert basket.title == "Базові продукти"

    def test_markup_is_stripped_from_lines_too(self) -> None:
        line = DraftLine(description="<b>молоко</b>", reason_text="бо <i>треба</i>")
        assert line.description == "молоко"
        assert line.reason_text == "бо треба"

    def test_whitespace_is_collapsed(self) -> None:
        assert DraftLine(description="молоко   2,6%\n\n", reason_text="  бо  ").description == (
            "молоко 2,6%"
        )

    def test_over_long_prose_is_truncated_not_rejected(self) -> None:
        """Losing a whole basket because the model was verbose would be worse than a
        clipped title."""
        basket = DraftBasket(
            title="я" * 500,
            intent="stated",
            lines=[DraftLine(description="молоко", reason_text="ви попросили")],
        )
        assert len(basket.title) == 200 and basket.title.endswith("…")
