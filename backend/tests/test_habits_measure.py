"""The Task 0 rules, checked against the trimmed history fixtures.

`scripts/_habits_measure.py` is the measure behind Plan 3's habits decision, kept as
code so the decision can be re-run. These tests pin the rules it states — netting,
`status`, removed lines, carrier bags, naive receipt times — against the real, trimmed
payloads in `tests/fixtures/mcp`, so a future `core/habits` normaliser has something to
agree with. Counts asserted here are counts *inside a frozen fixture*, never something
the clock or an account decides.
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _habits_measure import (
    collapse_same_day,
    cross_source_same_day,
    habits,
    is_bag,
    kyiv_day,
    offline_events,
    online_events,
    summary,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mcp"


@pytest.fixture(scope="module")
def online() -> dict:
    return json.loads((FIXTURES / "my_online_orders.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def offline() -> dict:
    return json.loads((FIXTURES / "my_offline_orders.json").read_text(encoding="utf-8"))


def test_canceled_orders_and_lineless_headers_yield_no_events(online: dict) -> None:
    events = online_events(online)
    delivered_with_lines = [
        o for o in online["orders"] if o["status"] == "received" and o["products"]
    ]
    assert {e.day for e in events} == {
        kyiv_day(o["delivery"]["deliveredAt"]) for o in delivered_with_lines
    }
    assert all(e.source == "online" for e in events)


def test_removed_online_line_is_not_a_purchase(online: dict) -> None:
    order = next(o for o in online["orders"] if o["status"] == "received" and o["products"])
    doctored = json.loads(json.dumps(order))
    doctored["products"][0]["removed"] = True
    before = len(online_events({"orders": [order]}))
    assert len(online_events({"orders": [doctored]})) == before - 1


def test_receipt_quantities_are_netted_and_corrections_cancel(offline: dict) -> None:
    receipt = next(
        r
        for r in offline["orders"]
        if any(p["quantity"] < 0 and p["unit"] == "кг" for p in r["products"])
    )
    corrected = next(p for p in receipt["products"] if p["quantity"] < 0)
    same = [p for p in receipt["products"] if p["lagerId"] == corrected["lagerId"]]
    expected = round(sum(p["quantity"] for p in same), 3)
    key = str(corrected["catalogProduct"]["id"])

    events = {e.key: e for e in offline_events({"orders": [receipt]})}
    assert round(events[key].qty, 3) == expected > 0


def test_a_product_returned_in_full_is_absent(offline: dict) -> None:
    receipt = next(
        r
        for r in offline["orders"]
        if any(p["quantity"] < 0 and p["unit"] != "кг" for p in r["products"])
    )
    returned = next(p for p in receipt["products"] if p["quantity"] < 0)
    net = sum(p["quantity"] for p in receipt["products"] if p["lagerId"] == returned["lagerId"])
    assert net == 0
    keys = {e.key for e in offline_events({"orders": [receipt]})}
    assert str(returned["catalogProduct"]["id"]) not in keys


def test_a_line_without_catalog_product_keys_on_its_lager_id(offline: dict) -> None:
    line = next(p for r in offline["orders"] for p in r["products"] if p["catalogProduct"] is None)
    keys = {e.key for e in offline_events(offline)}
    assert f"lager:{line['lagerId']}" in keys


def test_carrier_bags_are_named_first_not_mentioned() -> None:
    assert is_bag("Пакет біорозкладний 3кг 958358")
    assert is_bag("Пакет-майка Сільпо")
    assert not is_bag("Сир кисломолочний Ферма 5% пакет")
    assert not is_bag("Чай чорний 25 пакетиків")


def test_bags_never_become_events(offline: dict) -> None:
    bag = next(p for r in offline["orders"] for p in r["products"] if is_bag(p["name"]))
    keys = {e.key for e in offline_events(offline)}
    assert str(bag["catalogProduct"]["id"]) not in keys
    assert f"lager:{bag['lagerId']}" not in keys


def test_receipt_time_is_kyiv_and_online_time_is_converted() -> None:
    assert kyiv_day("2026-09-13T13:21:25") == date(2026, 9, 13)
    # 22:30 UTC is already the next day in Kyiv (UTC+3 in April).
    assert kyiv_day("2026-04-24T22:30:00+00:00") == date(2026, 4, 25)
    assert kyiv_day(None) is None
    assert kyiv_day("not a date") is None


def test_same_day_collapses_and_cross_source_pairs_are_counted() -> None:
    online_payload = {
        "orders": [
            {
                "status": "received",
                "delivery": {"deliveredAt": "2026-05-01T10:00:00+00:00"},
                "products": [{"id": "p1", "quantity": 2, "removed": False, "name": "Молоко"}],
            }
        ]
    }
    offline_payload = {
        "orders": [
            {
                "createdAt": "2026-05-01T18:00:00",
                "products": [
                    {"lagerId": 1, "name": "Молоко", "quantity": 1, "catalogProduct": {"id": "p1"}}
                ],
            }
        ]
    }
    events = online_events(online_payload) + offline_events(offline_payload)
    assert cross_source_same_day(events) == 1
    per_product = collapse_same_day(events)
    assert per_product == {"p1": {date(2026, 5, 1): 3.0}}


def test_habits_need_four_days_and_a_window_can_exclude_them() -> None:
    days = {date(2026, 1, d): 1.0 for d in (1, 8, 15, 22)}
    found = habits({"p1": days, "p2": {date(2026, 1, 1): 1.0}})
    assert [h.key for h in found] == ["p1"]
    assert found[0].median_gap == 7 and found[0].cv == 0
    assert habits({"p1": days}, since=date(2026, 1, 10)) == []


def test_summary_names_no_product(online: dict, offline: dict) -> None:
    text = summary(online, offline, today=date(2026, 9, 14))
    names = {p["name"] for o in online["orders"] for p in o["products"]}
    names |= {p["name"] for r in offline["orders"] for p in r["products"]}
    assert names and not any(name in text for name in names)
    assert "habits (receipt coverage)" in text
