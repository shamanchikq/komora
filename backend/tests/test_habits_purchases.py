"""The normaliser, against the trimmed real payloads and the Task 0 rules.

Every assertion is a rule from reference §9. Counts are counts inside a frozen
fixture, never something the clock or an account decides.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from komora.core.habits.purchases import (
    KYIV,
    LAGER_PREFIX,
    is_bag,
    offline_purchases,
    online_purchases,
    parse_time,
    receipt_key,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mcp"


@pytest.fixture(scope="module")
def online() -> dict:
    return json.loads((FIXTURES / "my_online_orders.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def offline() -> dict:
    return json.loads((FIXTURES / "my_offline_orders.json").read_text(encoding="utf-8"))


class TestOnline:
    def test_only_received_orders_with_lines_yield_events(self, online: dict) -> None:
        events = online_purchases(online)
        wanted = {
            o["orderId"] for o in online["orders"] if o["status"] == "received" and o["products"]
        }
        assert {e.receipt_key for e in events} == {f"o:{k}" for k in wanted}
        assert all(e.source == "online" and e.reorderable for e in events)

    def test_the_event_time_is_delivery_not_placement(self, online: dict) -> None:
        order = next(o for o in online["orders"] if o["status"] == "received" and o["products"])
        event = next(e for e in online_purchases({"orders": [order]}))
        assert event.bought_at == parse_time(order["delivery"]["deliveredAt"])
        assert event.bought_at != parse_time(order["createdAt"])
        assert event.bought_at.tzinfo is not None

    def test_a_removed_line_is_not_a_purchase(self, online: dict) -> None:
        order = next(o for o in online["orders"] if o["status"] == "received" and o["products"])
        doctored = json.loads(json.dumps(order))
        doctored["products"][0]["removed"] = True
        assert len(online_purchases({"orders": [doctored]})) == (
            len(online_purchases({"orders": [order]})) - 1
        )

    def test_a_canceled_order_with_delivered_at_is_not_a_purchase(self, online: dict) -> None:
        order = next(o for o in online["orders"] if o["status"] == "received" and o["products"])
        doctored = json.loads(json.dumps(order))
        doctored["status"] = "canceled"
        assert doctored["delivery"]["deliveredAt"]  # set, and still not evidence
        assert online_purchases({"orders": [doctored]}) == []

    def test_price_is_hryvnias_and_a_fractional_quantity_is_kilograms(self, online: dict) -> None:
        events = online_purchases(online)
        pork = next(e for e in events if not float(e.qty).is_integer())
        assert pork.weighted and pork.unit == "кг"
        whole = next(e for e in events if float(e.qty).is_integer())
        assert not whole.weighted and whole.unit == ""
        line = next(
            p for o in online["orders"] for p in o["products"] if p["id"] == whole.product_key
        )
        assert whole.unit_price == Decimal(str(line["price"])).quantize(Decimal("0.01"))

    def test_the_same_product_twice_in_one_order_is_summed(self) -> None:
        order = {
            "orderId": "x",
            "status": "received",
            "delivery": {"deliveredAt": "2026-05-01T10:00:00+00:00"},
            "products": [
                {"id": "p", "name": "Молоко", "price": 40, "quantity": 2, "removed": False},
                {"id": "p", "name": "Молоко", "price": 40, "quantity": 3, "removed": False},
            ],
        }
        (event,) = online_purchases({"orders": [order]})
        assert event.qty == 5


class TestOffline:
    def test_quantities_are_netted_per_product(self, offline: dict) -> None:
        receipt = next(
            r
            for r in offline["orders"]
            if any(p["quantity"] < 0 and p["unit"] == "кг" for p in r["products"])
        )
        corrected = next(p for p in receipt["products"] if p["quantity"] < 0)
        expected = round(
            sum(p["quantity"] for p in receipt["products"] if p["lagerId"] == corrected["lagerId"]),
            3,
        )
        events = {e.product_key: e for e in offline_purchases({"orders": [receipt]})}
        event = events[str(corrected["catalogProduct"]["id"])]
        assert event.qty == expected > 0
        assert event.weighted and event.unit == "кг"

    def test_a_full_return_is_no_purchase(self, offline: dict) -> None:
        receipt = next(
            r
            for r in offline["orders"]
            if any(p["quantity"] < 0 and p["unit"] != "кг" for p in r["products"])
        )
        returned = next(p for p in receipt["products"] if p["quantity"] < 0)
        keys = {e.product_key for e in offline_purchases({"orders": [receipt]})}
        assert str(returned["catalogProduct"]["id"]) not in keys

    def test_a_line_with_no_catalog_product_is_countable_but_not_reorderable(
        self, offline: dict
    ) -> None:
        line = next(
            p for r in offline["orders"] for p in r["products"] if p["catalogProduct"] is None
        )
        event = next(
            e
            for e in offline_purchases(offline)
            if e.product_key == f"{LAGER_PREFIX}{line['lagerId']}"
        )
        assert not event.reorderable
        assert event.external_product_id == line["lagerId"]

    def test_lager_id_is_kept_as_the_article_number(self, offline: dict) -> None:
        line = next(
            p
            for r in offline["orders"]
            for p in r["products"]
            if p["catalogProduct"] is not None and not is_bag(p["name"]) and p["quantity"] > 0
        )
        event = next(
            e
            for e in offline_purchases(offline)
            if e.product_key == str(line["catalogProduct"]["id"])
        )
        assert event.external_product_id == line["lagerId"]
        assert event.reorderable

    def test_pack_size_survives_as_the_unit(self, offline: dict) -> None:
        units = {e.unit for e in offline_purchases(offline) if not e.weighted}
        assert any(u.endswith("г") for u in units), units

    def test_receipt_time_is_kyiv(self, offline: dict) -> None:
        receipt = offline["orders"][0]
        event = offline_purchases({"orders": [receipt]})[0]
        local = datetime.fromisoformat(receipt["createdAt"]).replace(tzinfo=KYIV)
        assert event.bought_at == local.astimezone(UTC)

    def test_bags_never_become_events(self, offline: dict) -> None:
        bag = next(p for r in offline["orders"] for p in r["products"] if is_bag(p["name"]))
        keys = {e.product_key for e in offline_purchases(offline)}
        assert str(bag["catalogProduct"]["id"]) not in keys

    def test_nothing_personal_is_kept(self, offline: dict) -> None:
        receipt = offline["orders"][0]
        for event in offline_purchases({"orders": [receipt]}):
            for value in (event.receipt_key, event.name, event.unit):
                assert receipt["receiptUrl"] not in value
                assert receipt["filialName"] not in value
                assert receipt["cityName"] not in value


class TestRules:
    def test_bags_are_named_first_not_mentioned(self) -> None:
        assert is_bag("Пакет біорозкладний 3кг 958358")
        assert is_bag("Пакет-майка Сільпо")
        assert not is_bag("Сир кисломолочний Ферма 5% пакет")
        assert not is_bag("Чай чорний 25 пакетиків")

    def test_receipt_key_is_a_hash_of_the_url_token_with_a_fallback(self) -> None:
        by_url = receipt_key({"receiptUrl": "https://receipt.silpo.elkasa.com.ua/ABC123"})
        assert by_url is not None and by_url.startswith("r:") and "ABC123" not in by_url
        assert by_url == receipt_key({"receiptUrl": "https://other.host/ABC123"})
        assert receipt_key({"filId": 3746, "createdAt": "2026-09-13T13:21:25"}) == (
            "f:3746:2026-09-13T13:21:25"
        )
        assert receipt_key({}) is None

    def test_parse_time_localises_naive_stamps_to_kyiv(self) -> None:
        naive = parse_time("2026-09-13T13:21:25")
        assert naive is not None and naive.tzinfo is UTC
        assert naive.hour == 10  # 13:21 Kyiv (UTC+3) is 10:21 UTC
        assert parse_time("2026-04-24T16:34:49+00:00") == datetime(
            2026, 4, 24, 16, 34, 49, tzinfo=UTC
        )
        assert parse_time(None) is None and parse_time("nope") is None
