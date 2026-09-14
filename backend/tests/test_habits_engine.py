"""The engine's rules on synthetic histories — spec §6 as Plan 3 settled them."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from komora.core.habits.draft import habit_lines, suggested_qty
from komora.core.habits.engine import (
    LAPSE_GAPS,
    MIN_EVENTS,
    NUDGE_CV,
    TRACK_CV,
    Habit,
    compute_habits,
    coverage_start,
    due,
    due_to_buy,
    next_due,
)
from komora.core.habits.purchases import PurchaseEvent


def event(
    key: str,
    day: date,
    *,
    qty: float = 1,
    source: str = "offline",
    name: str = "Молоко",
    weighted: bool = False,
    reorderable: bool = True,
) -> PurchaseEvent:
    return PurchaseEvent(
        source="online" if source == "online" else "offline",
        receipt_key=f"{source}:{day.isoformat()}",
        product_key=key,
        name=name,
        qty=qty,
        unit="кг" if weighted else "900г",
        unit_price=Decimal("40.00"),
        weighted=weighted,
        reorderable=reorderable,
        bought_at=datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
        external_product_id=None,
    )


def weekly(
    key: str, weeks: int, start: date = date(2026, 6, 1), **kw: object
) -> list[PurchaseEvent]:
    return [event(key, start + timedelta(days=7 * i), **kw) for i in range(weeks)]  # type: ignore[arg-type]


def test_fewer_than_four_days_is_nothing() -> None:
    assert compute_habits(weekly("p", MIN_EVENTS - 1)) == []
    assert len(compute_habits(weekly("p", MIN_EVENTS))) == 1


def test_same_day_purchases_collapse_to_one_event() -> None:
    day = date(2026, 6, 1)
    twice = [event("p", day, qty=1), event("p", day, qty=2)]
    assert compute_habits(twice + weekly("p", 3, start=day + timedelta(days=7))) != []
    only = compute_habits(twice)
    assert only == []  # two receipts on one day are one event, not two


def test_across_sources_one_day_is_one_event_and_the_larger_quantity_wins() -> None:
    days = [date(2026, 6, 1) + timedelta(days=7 * i) for i in range(4)]
    events = [event("p", d, qty=2, source="offline") for d in days]
    events.append(event("p", days[-1], qty=5, source="online"))  # the delivery's receipt
    (habit,) = compute_habits(events)
    assert habit.events == 4
    assert habit.last_qty == 5  # max, not 7


def test_interval_is_the_median_so_a_bulk_gap_does_not_distort() -> None:
    start = date(2026, 6, 1)
    days = [
        start,
        start + timedelta(7),
        start + timedelta(14),
        start + timedelta(21),
        start + timedelta(49),
    ]
    (habit,) = compute_habits([event("p", d) for d in days])
    assert habit.median_gap_days == 7


def test_the_cv_gate_returns_nothing_not_a_weak_habit() -> None:
    start = date(2026, 6, 1)
    erratic = [start + timedelta(days=d) for d in (0, 1, 40, 41, 120)]
    assert compute_habits([event("p", d) for d in erratic]) == []
    (steady,) = compute_habits(weekly("p", 6))
    assert steady.cv <= NUDGE_CV <= TRACK_CV


def test_next_due_scales_with_the_last_quantity_within_bounds() -> None:
    last = date(2026, 6, 1)
    assert next_due(last, 7, 1, 1) == last + timedelta(days=7)
    assert next_due(last, 7, 2, 1) == last + timedelta(days=14)
    assert next_due(last, 7, 10, 1) == last + timedelta(days=14)  # clamped at ×2
    assert next_due(last, 7, 0.1, 1) == last + timedelta(days=4)  # clamped at ×0.5, 3.5 -> 4
    assert next_due(last, 7, 1, 0) == last + timedelta(days=7)  # no median: no scaling


def test_due_lists_only_due_nudgeable_unmuted_soonest_first() -> None:
    a = compute_habits(weekly("a", 5, name="А"))[0]
    # A week ahead of `a`: overdue on `a`'s due date, but well inside its lapse window.
    b = compute_habits(weekly("b", 5, start=date(2026, 5, 25), name="Б"))[0]
    today = a.due_on
    assert [h.product_key for h in due([a, b], today)] == ["b", "a"]
    muted = Habit(**{**a.__dict__, "muted": True})
    assert due([muted, b], today) == [b]
    counter = Habit(**{**a.__dict__, "reorderable": False})
    assert due([counter], today) == []
    assert due([a], today - timedelta(days=1)) == []


def test_the_sentence_is_about_receipts_and_always_true() -> None:
    (habit,) = compute_habits(weekly("p", 5, name="Молоко Галичина"))
    text = habit.sentence(habit.last_bought + timedelta(days=3))
    assert text == "Ви купуєте «Молоко Галичина» кожні ~7 днів, минуло 3 дні"
    assert "холодильник" not in text


def test_gaps_are_measured_inside_the_window_only() -> None:
    old = weekly("p", 4, start=date(2025, 1, 1))
    recent = weekly("p", 4, start=date(2026, 6, 1))
    # Over all history the year-long hole makes the rhythm erratic: no habit, rather
    # than «every ~7 days» built on a gap where no source had lines.
    assert compute_habits(old + recent) == []
    (windowed,) = compute_habits(old + recent, since=date(2026, 1, 1))
    assert windowed.events == 4 and windowed.median_gap_days == 7


def test_coverage_starts_at_the_first_receipt_so_a_data_hole_is_not_a_cadence() -> None:
    """The live artefact: online orders a year apart, then receipts. Before the first
    receipt the in-store purchases are invisible, so those gaps are not measured."""
    online = [
        event("p", date(2025, 2, 25), source="online"),
        event("p", date(2025, 7, 20), source="online"),
        event("p", date(2025, 12, 13), source="online"),
    ]
    receipts = weekly("p", 4, start=date(2026, 6, 24)) + weekly("q", 4, start=date(2026, 6, 24))
    assert coverage_start(online + receipts) == date(2026, 6, 24)
    found = {h.product_key: h for h in compute_habits(online + receipts)}
    assert found["p"].events == 4 and found["p"].median_gap_days == 7
    # Online only: coverage is the first order with lines, and the gaps are real.
    (only_online,) = compute_habits([*online, event("p", date(2026, 4, 24), source="online")])
    assert only_online.events == 4


def test_habit_lines_carry_the_sentence_and_skip_muted_and_counter_goods() -> None:
    (a,) = compute_habits(weekly("a", 5, name="Кефір"))
    (b,) = compute_habits(weekly("b", 5, name="Форель", weighted=True, qty=0.7))
    counter = Habit(**{**b.__dict__, "product_key": "lager:1", "reorderable": False})
    muted = Habit(**{**a.__dict__, "product_key": "m", "muted": True})
    today = date(2026, 7, 10)
    lines = habit_lines([a, b, counter, muted], today)
    assert [ln.product_id for ln in lines] == ["a", "b"]
    assert lines[0].reason_kind == "habit"
    assert lines[0].reason_text == a.sentence(today)
    assert lines[0].quantity == 1
    assert lines[1].quantity == 0.7


def test_suggested_qty_is_whole_packs_or_kilograms_never_zero() -> None:
    (a,) = compute_habits(weekly("a", 5, qty=2))
    assert suggested_qty(a) == 2
    (b,) = compute_habits(weekly("b", 5, qty=0.35, weighted=True))
    assert suggested_qty(b) == 0.35
    (c,) = compute_habits(weekly("c", 5, qty=0.0001, weighted=True))
    assert suggested_qty(c) == 0.1


def test_a_long_overdue_habit_has_lapsed_and_is_not_due() -> None:
    """Weekly milk last seen in May is not «due» in September — it stopped, or it is
    bought where Komora cannot see. Either way nothing may be sent about it."""
    (habit,) = compute_habits(weekly("p", 5))
    window = int(LAPSE_GAPS * habit.median_gap_days)
    assert habit.is_due(habit.due_on + timedelta(days=window))
    gone = habit.due_on + timedelta(days=window + 1)
    assert habit.lapsed(gone) and not habit.is_due(gone)
    assert due([habit], gone) == []
    assert due_to_buy([habit], gone) == []
    # Still true to say, so it still has a sentence.
    assert "минуло" in habit.sentence(gone)


def test_due_to_buy_ignores_the_nudge_tier_but_not_the_date() -> None:
    (a,) = compute_habits(weekly("a", 5))
    loose = Habit(**{**a.__dict__, "product_key": "loose", "cv": NUDGE_CV + 0.1})
    today = a.due_on
    assert due([a, loose], today) == [a]
    assert {h.product_key for h in due_to_buy([a, loose], today)} == {"a", "loose"}
    assert due_to_buy([a], today - timedelta(days=1)) == []
    muted = Habit(**{**a.__dict__, "muted": True})
    counter = Habit(**{**a.__dict__, "reorderable": False})
    assert due_to_buy([muted, counter], today) == []


def test_a_later_delivery_does_not_erase_what_the_receipts_knew() -> None:
    """An online line has no article number and no unit. The newest event used to
    supply both, so one delivery sent the next draft searching by name."""
    receipts = [replace(e, external_product_id=815253) for e in weekly("p", 4)]
    delivery = event("p", date(2026, 6, 29), source="online", weighted=True)
    (habit,) = compute_habits([*receipts, delivery])
    assert habit.external_product_id == 815253
    assert habit.unit == "900г" and not habit.weighted
