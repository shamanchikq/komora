"""Purchase events become habits — or nothing.

The rules are spec §6 as Plan 3 settled them, on the catalog product id:

* Same-day purchases of one product are one event. Across sources too: an online
  delivery that also produced a receipt (Task 0 #5, still untested) must count once,
  so the collapse runs after the sources are merged. That day's quantity is the
  **largest** a source reported, not the sum.
* **≥ 4 events** before a product exists here at all.
* Interval = **median** gap in days. Bulk buys must not distort it.
* Confidence is a function of event count and the coefficient of variation of the
  gaps. Below the threshold the engine returns **nothing** for that product — not a
  low-confidence habit, no habit.
* The next expected purchase scales with the last quantity bought, within reason.

**The thresholds, decided 2026-09-14 on one household.** Tracked at CV ≤ 1.0 — shown
in the payoff and in «your usual» — and *nudged* at CV ≤ 0.75, because a message
Komora sends unasked has a higher cost of being wrong than a list the user opened. On
the history measured that is 3–5 tracked and 1–2 nudged; CV ≤ 0.5 tracked nothing at
all. Both numbers are expected to move once a second household is measured.

**The sentence is about receipts, never the fridge.** «Ви купуєте X кожні ~N днів,
минуло M» is built here, deterministically, and is always true.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise

from komora.core.habits.purchases import KYIV, PurchaseEvent
from komora.core.text import days

MIN_EVENTS = 4
TRACK_CV = 1.0
NUDGE_CV = 0.75
QTY_SCALE = (0.5, 2.0)
"""How far the last quantity may stretch or shrink the next expected gap."""


@dataclass(frozen=True)
class Habit:
    product_key: str
    name: str
    events: int
    median_gap_days: float
    cv: float
    confidence: float
    last_bought: date
    last_qty: float
    median_qty: float
    due_on: date
    unit: str = ""
    weighted: bool = False
    reorderable: bool = True
    external_product_id: int | None = None
    muted: bool = False
    """User state, joined in by the repository; the engine never sets it."""

    @property
    def nudgeable(self) -> bool:
        return self.cv <= NUDGE_CV and self.reorderable and not self.muted

    def days_since(self, today: date) -> int:
        return max(0, (today - self.last_bought).days)

    def is_due(self, today: date) -> bool:
        return today >= self.due_on

    def sentence(self, today: date) -> str:
        """«Ви купуєте X кожні ~N днів, минуло M» — the one claim the data supports."""
        return (
            f"Ви купуєте «{self.name}» кожні ~{days(round(self.median_gap_days))}, "
            f"минуло {days(self.days_since(today))}"
        )


def kyiv_day(when: datetime) -> date:
    return when.astimezone(KYIV).date()


def _collapse(events: list[PurchaseEvent]) -> dict[str, dict[date, float]]:
    per_source: dict[tuple[str, date, str], float] = defaultdict(float)
    for event in events:
        per_source[(event.product_key, kyiv_day(event.bought_at), event.source)] += event.qty
    per_day: dict[str, dict[date, float]] = defaultdict(dict)
    for (key, day, _), qty in per_source.items():
        per_day[key][day] = max(per_day[key].get(day, 0.0), qty)
    return per_day


def _cv(gaps: list[int]) -> float:
    mean = statistics.fmean(gaps)
    return statistics.pstdev(gaps) / mean if mean else float("inf")


def confidence_of(events: int, cv: float) -> float:
    """A number for ordering, not the gate. The gate is the CV cut in `compute_habits`."""
    return round(max(0.0, 1 - cv / TRACK_CV) * min(1.0, events / 8), 3)


def next_due(last_bought: date, median_gap: float, last_qty: float, median_qty: float) -> date:
    """Expected next purchase, stretched by how much was bought last time.

    `next_due = last_bought + median_gap × clamp(last_qty / median_qty, 0.5, 2)`: a double
    pack pushes the expectation out by at most one extra interval, a half pack pulls it
    in by at most half.
    """
    ratio = last_qty / median_qty if median_qty > 0 else 1.0
    factor = min(QTY_SCALE[1], max(QTY_SCALE[0], ratio))
    return last_bought + timedelta(days=max(1, round(median_gap * factor)))


def coverage_start(events: list[PurchaseEvent]) -> date | None:
    """The first day from which purchases are actually observed.

    Receipts are where most shopping is, and they reach back only as far as the card
    has been used (80 days on the one account seen); before that, in-store purchases
    are invisible, and online orders older than 2025 are headers with no lines. A gap
    measured across that hole is a gap in the *data*, not in the shopping: on the real
    history it produced «Сир Ферма every ~146 days» — five purchases spanning a year in
    which ninety-three orders' contents are unknown. So gaps are measured from the
    first receipt when there are receipts, and from the first online order with lines
    otherwise. Verified on the live history 2026-09-14: the 146-day cheese drops out
    and the four habits that remain are all inside the receipts' own span.
    """
    offline = [e.bought_at for e in events if e.source == "offline"]
    pool = offline or [e.bought_at for e in events]
    return kyiv_day(min(pool)) if pool else None


def compute_habits(events: list[PurchaseEvent], *, since: date | None = None) -> list[Habit]:
    """Every product with a rhythm the rules accept; nothing for the rest.

    Gaps are measured only across observed coverage — `since` defaults to
    `coverage_start(events)`; pass a date to narrow further, never to widen.
    """
    if since is None:
        since = coverage_start(events)
    per_day = _collapse(events)
    latest: dict[str, PurchaseEvent] = {}
    for event in events:
        if event.product_key not in latest or event.bought_at > latest[event.product_key].bought_at:
            latest[event.product_key] = event

    found: list[Habit] = []
    for key, by_day in per_day.items():
        days_seen = sorted(d for d in by_day if since is None or d >= since)
        if len(days_seen) < MIN_EVENTS:
            continue
        gaps = [(b - a).days for a, b in pairwise(days_seen)]
        cv = _cv(gaps)
        if cv > TRACK_CV:
            continue
        median_gap = float(statistics.median(gaps))
        quantities = [by_day[d] for d in days_seen]
        median_qty = float(statistics.median(quantities))
        last_day = days_seen[-1]
        sample = latest[key]
        found.append(
            Habit(
                product_key=key,
                name=sample.name,
                events=len(days_seen),
                median_gap_days=median_gap,
                cv=round(cv, 3),
                confidence=confidence_of(len(days_seen), cv),
                last_bought=last_day,
                last_qty=by_day[last_day],
                median_qty=median_qty,
                due_on=next_due(last_day, median_gap, by_day[last_day], median_qty),
                unit=sample.unit,
                weighted=sample.weighted,
                reorderable=sample.reorderable,
                external_product_id=sample.external_product_id,
            )
        )
    return sorted(found, key=lambda h: (-h.confidence, h.due_on, h.name))


def due(habits: list[Habit], today: date) -> list[Habit]:
    """What a nudge may mention: due, nudgeable, and not muted. Soonest first."""
    return sorted((h for h in habits if h.is_due(today) and h.nudgeable), key=lambda h: h.due_on)
