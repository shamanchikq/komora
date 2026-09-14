"""Paging, `dateStart`, and the two-source contract of the importer, against the fake."""

from datetime import UTC, datetime, timedelta

from komora.core.habits.importer import (
    BACKFILL,
    ImportReport,
    date_start_for,
    import_history,
    read_offline,
    read_online,
)
from komora.core.habits.purchases import PurchaseEvent
from tests.fakes import CONTEXT, FakeSilpo


def order(i: int, *, day: str = "2026-06-01", lines: int = 1) -> dict:
    return {
        "orderId": f"o{i}",
        "status": "received",
        "createdAt": f"{day}T10:00:00+00:00",
        "delivery": {"deliveredAt": f"{day}T12:00:00+00:00"},
        "products": [
            {"id": f"p{i}-{n}", "name": "Молоко", "price": 40, "quantity": 1, "removed": False}
            for n in range(lines)
        ],
    }


def receipt(i: int, day: str) -> dict:
    return {
        "createdAt": f"{day}T13:00:00",
        "filId": 1,
        "receiptUrl": f"https://receipt.silpo.elkasa.com.ua/T{i}",
        "products": [
            {
                "lagerId": 100 + i,
                "name": "Кефір",
                "price": 30.0,
                "quantity": 1,
                "unit": "870г",
                "catalogProduct": {"id": f"c{i}", "weighted": False},
            }
        ],
    }


class Sink:
    def __init__(self, fail: bool = False) -> None:
        self.stored: list[PurchaseEvent] = []
        self._fail = fail

    async def upsert(self, user_id: int, events: list[PurchaseEvent]) -> int:
        if self._fail:
            raise RuntimeError("db down")
        self.stored.extend(events)
        return len(events)


async def test_online_pages_fifty_at_a_time_until_total() -> None:
    silpo = FakeSilpo(online_orders=[order(i) for i in range(120)])
    events = await read_online(silpo, stop_before=None)
    assert len(events) == 120
    assert [c[1]["offset"] for c in silpo.history_calls] == [0, 50, 100]
    assert all(c[1]["limit"] == 50 for c in silpo.history_calls)


async def test_online_stops_at_the_first_page_older_than_the_last_import() -> None:
    recent = [order(i, day="2026-08-01") for i in range(50)]
    old = [order(100 + i, day="2025-01-01") for i in range(50)]
    ancient = [order(200 + i, day="2024-01-01") for i in range(50)]
    silpo = FakeSilpo(online_orders=recent + old + ancient)
    await read_online(silpo, stop_before=datetime(2025, 6, 1, tzinfo=UTC))
    # The second page already predates the cutoff, so the third is never fetched.
    assert [c[1]["offset"] for c in silpo.history_calls] == [0, 50]


async def test_offline_pages_ten_at_a_time_with_date_start() -> None:
    receipts = [receipt(i, f"2026-08-{1 + i % 28:02d}") for i in range(23)]
    silpo = FakeSilpo(offline_orders=receipts)
    now = datetime(2026, 9, 14, tzinfo=UTC)
    events = await read_offline(silpo, CONTEXT, since=None, now=now)
    assert len(events) == 23
    calls = [c for c in silpo.history_calls if c[0] == "offline"]
    assert [c[1]["offset"] for c in calls] == [0, 10, 20]
    assert all(c[1]["limit"] == 10 for c in calls)
    assert all(c[1]["dateStart"] == date_start_for(None, now) for c in calls)


def test_date_start_is_the_last_import_minus_a_day_or_the_backfill_horizon() -> None:
    now = datetime(2026, 9, 14, 12, tzinfo=UTC)
    assert date_start_for(datetime(2026, 9, 10, 8, tzinfo=UTC), now) == "2026-09-09T00:00:00"
    assert date_start_for(None, now) == (now - BACKFILL).strftime("%Y-%m-%dT00:00:00")


async def test_a_refresh_sends_date_start_and_the_fake_honours_it() -> None:
    silpo = FakeSilpo(offline_orders=[receipt(1, "2026-07-01"), receipt(2, "2026-09-10")])
    now = datetime(2026, 9, 14, tzinfo=UTC)
    events = await read_offline(silpo, CONTEXT, since=datetime(2026, 9, 1, tzinfo=UTC), now=now)
    assert [e.product_key for e in events] == ["c2"]


async def test_without_context_receipts_are_skipped_and_said_so() -> None:
    silpo = FakeSilpo(online_orders=[order(1)], offline_orders=[receipt(1, "2026-09-01")])
    sink = Sink()
    report = await import_history(
        silpo, sink, 1, context=None, since_online=None, since_offline=None
    )
    assert report == ImportReport(online=1, offline=None, skipped=report.skipped, errors=[])
    assert report.skipped and "timeslot" in report.skipped
    assert all(e.source == "online" for e in sink.stored)


async def test_one_source_failing_does_not_stop_the_other() -> None:
    silpo = FakeSilpo(
        online_orders=[order(1)],
        offline_orders=[receipt(1, "2026-09-01")],
        fails={"get_my_online_orders"},
    )
    sink = Sink()
    report = await import_history(
        silpo, sink, 1, context=CONTEXT, since_online=None, since_offline=None
    )
    assert report.online is None and report.offline == 1
    assert not report.ok and report.errors[0].startswith("online:")
    assert [e.source for e in sink.stored] == ["offline"]


async def test_a_failing_sink_is_an_error_not_a_crash() -> None:
    silpo = FakeSilpo(online_orders=[order(1)])
    report = await import_history(
        silpo, Sink(fail=True), 1, context=None, since_online=None, since_offline=None
    )
    assert report.online is None and "db down" in report.errors[0]


async def test_a_lying_total_cannot_spin_forever() -> None:
    class Liar(FakeSilpo):
        async def get_my_online_orders(self, *, limit: int = 50, offset: int = 0) -> dict:
            return {"orders": [order(offset)], "meta": {"total": 10**9}}

    events = await read_online(Liar(), stop_before=None)
    assert 0 < len(events) <= 40


async def test_orders_older_than_one_day_before_last_import_are_still_read_once() -> None:
    """The margin: a refresh re-reads the last day, so a delivery that landed after
    the previous import on the same day is not lost."""
    silpo = FakeSilpo(online_orders=[order(1, day="2026-09-13")])
    since = datetime(2026, 9, 13, 23, tzinfo=UTC)
    events = await read_online(silpo, stop_before=since - timedelta(days=1))
    assert len(events) == 1
