"""Sync: the hand-off from Komora's draft to the user's real Silpo cart.

Two facts confirmed live (spec §3.1) shape all of it:

* adding **appends** — the user's existing lines survive;
* re-adding a product **sets** its quantity rather than incrementing it, which makes a
  retried sync idempotent but means overlapping products do **not** sum.
"""

from decimal import Decimal

from komora.core.models import ResolvedCart, ResolvedLine
from komora.core.sync import execute_sync, preview_sync
from tests.fakes import COMPANY, CONTEXT, FakeSilpo


def line(name: str, price: str, qty: float = 1, **kw: object) -> ResolvedLine:
    return ResolvedLine.model_validate(
        {
            "product_id": f"id-{name}",
            "company_id": COMPANY,
            "branch_id": CONTEXT.branch_id,
            "name": name,
            "qty": qty,
            "unit": "900г",
            "unit_price": Decimal(price),
            "reason_kind": "stated",
            "reason_text": "ви попросили",
        }
        | kw
    )


def cart(*lines: ResolvedLine) -> ResolvedCart:
    return ResolvedCart(
        lines=list(lines),
        total=sum((ln.line_total for ln in lines if not ln.unavailable), Decimal("0")),
    )


def in_silpo(name: str, price: float = 10, qty: float = 1) -> dict:
    return {
        "productId": f"id-{name}",
        "companyId": COMPANY,
        "branchId": CONTEXT.branch_id,
        "name": name,
        "price": price,
        "quantity": qty,
    }


class TestPreview:
    async def test_reports_what_is_already_in_the_silpo_cart(self) -> None:
        """The confirm sheet promises nothing will be removed, so it must show what
        is there."""
        mcp = FakeSilpo(existing=[in_silpo("Молоко", 42.90), in_silpo("Хліб", 28.50)])
        preview = await preview_sync(cart(line("Кава", "164.90")), mcp)
        assert preview.existing_count == 2
        assert preview.existing_total == Decimal("71.40")
        assert preview.adding_count == 1

    async def test_empty_silpo_cart(self) -> None:
        preview = await preview_sync(cart(line("Кава", "164.90")), FakeSilpo())
        assert preview.existing_count == 0
        assert preview.existing_total == Decimal("0")

    async def test_unavailable_lines_are_not_counted_as_being_added(self) -> None:
        preview = await preview_sync(
            cart(line("Кава", "164.90"), line("Ікра", "900", unavailable=True)), FakeSilpo()
        )
        assert preview.adding_count == 1

    async def test_overlapping_products_are_flagged(self) -> None:
        """Quantity is SET, not incremented — so the sheet must not promise addition
        for a product the user already has."""
        mcp = FakeSilpo(existing=[in_silpo("Молоко", 42.90, qty=1)])
        preview = await preview_sync(cart(line("Молоко", "42.90", qty=2)), mcp)
        assert preview.overlapping == ["Молоко"]

    async def test_no_overlap_when_products_differ(self) -> None:
        mcp = FakeSilpo(existing=[in_silpo("Хліб")])
        assert (await preview_sync(cart(line("Молоко", "42.90")), mcp)).overlapping == []

    async def test_blocking_validations_are_surfaced(self) -> None:
        """Silpo's own docs: errors in calculation.validations[] block checkout."""
        mcp = FakeSilpo(
            validations=[
                {"level": "error", "message": "Мінімальна сума замовлення 200 ₴"},
                {"level": "info", "message": "Акція діє до неділі"},
            ]
        )
        preview = await preview_sync(cart(line("Кава", "164.90")), mcp)
        assert preview.blocking_validations == ["Мінімальна сума замовлення 200 ₴"]

    async def test_final_total_uses_what_the_user_actually_pays(self) -> None:
        """cart.calculation.totalAfterDiscounts, not `total` — Silpo is explicit."""
        mcp = FakeSilpo(existing=[in_silpo("Молоко", 42.90)])
        preview = await preview_sync(cart(line("Кава", "164.90")), mcp)
        assert preview.existing_total == Decimal("42.90")


class TestExecute:
    async def test_happy_path_adds_everything_and_returns_a_checkout_link(self) -> None:
        mcp = FakeSilpo()
        report = await execute_sync(cart(line("Молоко", "42.90"), line("Хліб", "28.50")), mcp)
        assert report.ok is True
        assert sorted(report.added) == ["Молоко", "Хліб"]
        assert report.failed == []
        assert report.checkout_web_link == "https://silpo.ua/checkout/abc"

    async def test_existing_lines_are_untouched(self) -> None:
        """The promise the confirm sheet makes, verified against the fake's A1 rules."""
        mcp = FakeSilpo(existing=[in_silpo("Морозиво", 139)])
        await execute_sync(cart(line("Молоко", "42.90")), mcp)
        after = await mcp.get_shopping_cart_by_id("x")
        ids = {p["productId"] for p in after["cart"]["shipments"][0]["products"]}
        assert ids == {"id-Морозиво", "id-Молоко"}

    async def test_only_the_four_declared_fields_are_sent(self) -> None:
        """`silpo_add_or_update_cart_products` declares productId, companyId, branchId
        and quantity. Nothing else was ever sent to the live server, so nothing else
        goes now."""
        mcp = FakeSilpo()
        await execute_sync(cart(line("Молоко", "42.90", qty=2)), mcp)
        assert mcp.add_calls == [
            [
                {
                    "productId": "id-Молоко",
                    "companyId": COMPANY,
                    "branchId": CONTEXT.branch_id,
                    "quantity": 2,
                }
            ]
        ]

    async def test_unavailable_lines_are_never_sent(self) -> None:
        mcp = FakeSilpo()
        await execute_sync(
            cart(line("Молоко", "42.90"), line("Ікра", "900", unavailable=True)), mcp
        )
        sent = [p["productId"] for batch in mcp.add_calls for p in batch]
        assert sent == ["id-Молоко"]

    async def test_partial_failure_is_never_reported_as_success(self) -> None:
        mcp = FakeSilpo(reject={"id-Ікра"})
        report = await execute_sync(cart(line("Молоко", "42.90"), line("Ікра", "900")), mcp)
        assert report.ok is False
        assert report.added == ["Молоко"]
        assert [name for name, _ in report.failed] == ["Ікра"]

    async def test_a_rejected_batch_falls_back_to_individual_sends(self) -> None:
        """One bad product must not cost the user the whole basket."""
        mcp = FakeSilpo(reject={"id-Ікра"})
        report = await execute_sync(
            cart(line("Молоко", "42.90"), line("Ікра", "900"), line("Хліб", "28.50")), mcp
        )
        assert sorted(report.added) == ["Молоко", "Хліб"]
        assert len(mcp.add_calls) > 1, "should retry item by item after the batch failed"

    async def test_a_cart_that_cannot_be_checked_out_reports_why(self) -> None:
        """Silpo issues no `checkoutWebLink` for a cart with a blocking validation, so
        the report has to carry the reason instead."""
        mcp = FakeSilpo(
            checkout_links=False,
            validations=[{"level": "error", "message": "product.offer.stock.max"}],
        )
        report = await execute_sync(cart(line("Молоко", "42.90")), mcp)
        assert report.ok is True
        assert report.checkout_web_link is None
        assert report.blocking_validations == ["product.offer.stock.max"]

    async def test_empty_cart_is_a_no_op(self) -> None:
        mcp = FakeSilpo()
        report = await execute_sync(cart(), mcp)
        assert report.added == [] and report.failed == []
        assert mcp.add_calls == []

    async def test_report_is_built_from_a_re_read_not_from_the_response(self) -> None:
        """A write that answers "success" and adds nothing is reported as a failure.

        Ground truth is what is in the cart afterwards, which is the only thing the
        user can check.
        """
        mcp = FakeSilpo(swallow={"id-Ікра"})
        report = await execute_sync(cart(line("Молоко", "42.90"), line("Ікра", "900")), mcp)
        assert report.ok is False
        assert report.added == ["Молоко"]
        assert [name for name, _ in report.failed] == ["Ікра"]


class TestIdempotency:
    async def test_re_running_a_sync_does_not_double_quantities(self) -> None:
        """Quantity is SET, so a retry is safe by construction — the property the
        partial-failure path depends on."""
        mcp = FakeSilpo()
        draft = cart(line("Молоко", "42.90", qty=2))
        await execute_sync(draft, mcp)
        await execute_sync(draft, mcp)
        products = (await mcp.get_shopping_cart_by_id("x"))["cart"]["shipments"][0]["products"]
        assert len(products) == 1
        assert products[0]["quantity"] == 2, "set, not incremented to 4"

    async def test_retry_after_partial_failure_completes_the_cart(self) -> None:
        mcp = FakeSilpo(reject={"id-Ікра"})
        draft = cart(line("Молоко", "42.90"), line("Ікра", "900"))
        first = await execute_sync(draft, mcp)
        assert first.ok is False

        mcp._reject = set()  # the transient problem clears
        second = await execute_sync(draft, mcp)
        assert second.ok is True
        assert sorted(second.added) == ["Ікра", "Молоко"]

    async def test_overlapping_quantity_is_replaced_not_summed(self) -> None:
        mcp = FakeSilpo(existing=[in_silpo("Молоко", 42.90, qty=1)])
        await execute_sync(cart(line("Молоко", "42.90", qty=3)), mcp)
        products = (await mcp.get_shopping_cart_by_id("x"))["cart"]["shipments"][0]["products"]
        assert products[0]["quantity"] == 3, "3, not 4"


class TestDrift:
    async def test_price_change_beyond_the_threshold_is_flagged(self) -> None:
        """The user confirmed a total; a materially different one needs re-confirming."""
        mcp = FakeSilpo(existing=[])
        stale = ResolvedCart(lines=[line("Молоко", "42.90")], total=Decimal("30.00"))
        preview = await preview_sync(stale, mcp)
        assert preview.drift is not None
        assert preview.drift == (Decimal("30.00"), Decimal("42.90"))

    async def test_small_change_is_tolerated(self) -> None:
        mcp = FakeSilpo()
        nearly = ResolvedCart(lines=[line("Молоко", "42.90")], total=Decimal("42.50"))
        assert (await preview_sync(nearly, mcp)).drift is None


class TestPrematureValidations:
    """Silpo computes validations against the cart as it stands — which, in a preview,
    is the cart *without* the lines about to be added."""

    async def test_a_minimum_order_complaint_is_not_shown_before_the_write(self) -> None:
        """Live: an empty cart reported «сума менша за мінімальну» to a user who was in
        the act of adding 2401 ₴ of food."""
        mcp = FakeSilpo(validations=[{"level": "error", "message": "order.cost.min"}])
        preview = await preview_sync(cart(line("Кава", "2401.98")), mcp)
        assert preview.blocking_validations == []

    async def test_it_is_still_shown_when_nothing_is_being_added(self) -> None:
        mcp = FakeSilpo(validations=[{"level": "error", "message": "order.cost.min"}])
        preview = await preview_sync(cart(), mcp)
        assert preview.blocking_validations == ["order.cost.min"]

    async def test_state_based_validations_survive(self) -> None:
        """An expired slot or a line over stock is true regardless of what is added."""
        mcp = FakeSilpo(
            validations=[
                {"level": "error", "message": "timeslot.not_available"},
                {"level": "error", "message": "product.offer.stock.max"},
                {"level": "error", "message": "order.cost.min"},
            ]
        )
        preview = await preview_sync(cart(line("Кава", "164.90")), mcp)
        assert preview.blocking_validations == [
            "timeslot.not_available",
            "product.offer.stock.max",
        ]

    async def test_the_report_after_the_write_keeps_everything(self) -> None:
        """By then the cart is complete, so the check is meaningful again."""
        mcp = FakeSilpo(
            checkout_links=False,
            validations=[{"level": "error", "message": "order.cost.min"}],
        )
        report = await execute_sync(cart(line("Кава", "10.00")), mcp)
        assert report.blocking_validations == ["order.cost.min"]
