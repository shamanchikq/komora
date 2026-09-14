"""The Task 0 measure behind Plan 3's habits decision, as code that can be re-run.

The numbers in `docs/silpo-mcp-reference.md` §9 ("Task 0 answers") were first produced
by throwaway code over payloads that were then deleted, which left the decision they
support unreproducible. This module is that code, kept: pure functions from the two
history payloads to counts. It is **not** the engine — `core/habits/engine.py` is
Plan 3 Task 2 and owns the real rules — but it applies the same rules the plan states,
so a future engine can be checked against it on the same fixtures.

Nothing here prints a product name. Counts, dates and keys only.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
"""Receipt times carry no offset (reference §9); a Silpo till is in Ukraine."""

DELIVERED = "received"
"""The only `status` that means an online order was delivered. `deliveredAt` is set
on canceled orders too (Task 0 #10)."""

BAG_WORDS = frozenset({"пакет", "пакунок"})
"""A carrier bag is a product whose *first* word is «Пакет» (Task 0; the substring rule
rejected cottage cheese sold in a bag)."""

MIN_EVENTS = 4
CV_CUTS = (0.5, 0.75, 1.0)


@dataclass(frozen=True)
class Event:
    key: str
    day: date
    qty: float
    source: str  # "online" | "offline"


def _first_word(name: Any) -> str:
    text = str(name or "").strip().lower()
    return text.replace("-", " ").split()[0] if text else ""


def is_bag(name: Any) -> bool:
    return _first_word(name) in BAG_WORDS


def kyiv_day(stamp: Any) -> date | None:
    """The calendar day in Kyiv of an ISO timestamp, with or without an offset."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KYIV)
    return parsed.astimezone(KYIV).date()


def online_events(payload: dict[str, Any]) -> list[Event]:
    """One event per (product, order) from delivered online orders, removed lines dropped."""
    events: list[Event] = []
    for order in payload.get("orders") or []:
        if not isinstance(order, dict) or order.get("status") != DELIVERED:
            continue
        day = kyiv_day((order.get("delivery") or {}).get("deliveredAt"))
        if day is None:
            continue
        for line in order.get("products") or []:
            if not isinstance(line, dict) or line.get("removed") or is_bag(line.get("name")):
                continue
            key = str(line.get("id") or "")
            if key:
                events.append(Event(key, day, float(line.get("quantity") or 0), "online"))
    return events


def offline_events(payload: dict[str, Any]) -> list[Event]:
    """One event per (product, receipt), quantities netted across the receipt's lines.

    A till correction is a negative line (Task 0 #7); a product whose net quantity is
    not positive was not bought. A line with no `catalogProduct` keys on its `lagerId`
    so it still counts as a purchase, even though it can never become a cart line.
    """
    events: list[Event] = []
    for receipt in payload.get("orders") or []:
        if not isinstance(receipt, dict):
            continue
        day = kyiv_day(receipt.get("createdAt"))
        if day is None:
            continue
        net: dict[str, float] = defaultdict(float)
        for line in receipt.get("products") or []:
            if not isinstance(line, dict) or is_bag(line.get("name")):
                continue
            catalog = line.get("catalogProduct")
            key = (
                str(catalog.get("id"))
                if isinstance(catalog, dict) and catalog.get("id")
                else f"lager:{line.get('lagerId')}"
            )
            net[key] += float(line.get("quantity") or 0)
        events.extend(Event(k, day, q, "offline") for k, q in net.items() if q > 0)
    return events


def collapse_same_day(events: list[Event]) -> dict[str, dict[date, float]]:
    """Same-day purchases of one product are one event; quantity is the day's total."""
    days: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for event in events:
        days[event.key][event.day] += event.qty
    return {k: dict(v) for k, v in days.items()}


def cross_source_same_day(events: list[Event]) -> int:
    """(product, day) pairs seen from BOTH sources — a delivery that also produced a
    receipt would show here. Zero proves nothing unless the sources overlap in time."""
    seen: dict[tuple[str, date], set[str]] = defaultdict(set)
    for event in events:
        seen[(event.key, event.day)].add(event.source)
    return sum(1 for sources in seen.values() if len(sources) > 1)


def gaps_in_days(days: list[date]) -> list[int]:
    ordered = sorted(days)
    return [(b - a).days for a, b in pairwise(ordered)]


def cv_of(gaps: list[int]) -> float | None:
    if len(gaps) < 2:
        return None
    mean = statistics.fmean(gaps)
    return statistics.pstdev(gaps) / mean if mean else None


@dataclass(frozen=True)
class Habit:
    key: str
    events: int
    median_gap: float
    cv: float | None


def habits(per_product: dict[str, dict[date, float]], *, since: date | None = None) -> list[Habit]:
    """Products bought on at least MIN_EVENTS distinct days (optionally within a window).

    `since` bounds the days considered, so the same history can be measured over all
    time, the last year, or only the span both sources actually cover — the «every ~146
    days» artefact came from a gap where no source had lines at all.
    """
    out: list[Habit] = []
    for key, days in per_product.items():
        kept = [d for d in days if since is None or d >= since]
        if len(kept) < MIN_EVENTS:
            continue
        gaps = gaps_in_days(kept)
        out.append(Habit(key, len(kept), statistics.median(gaps), cv_of(gaps)))
    return sorted(out, key=lambda h: (h.cv if h.cv is not None else 9, -h.events))


def count_under(found: list[Habit], cut: float) -> int:
    return sum(1 for h in found if h.cv is not None and h.cv <= cut)


def span(events: list[Event]) -> tuple[date, date] | None:
    days = [e.day for e in events]
    return (min(days), max(days)) if days else None


def summary(online: dict[str, Any], offline: dict[str, Any], today: date | None = None) -> str:
    """The whole measure as printable lines. No names, no prices."""
    today = today or datetime.now(UTC).astimezone(KYIV).date()
    on, off = online_events(online), offline_events(offline)
    orders = [o for o in (online.get("orders") or []) if isinstance(o, dict)]
    receipts = [r for r in (offline.get("orders") or []) if isinstance(r, dict)]

    with_lines = [o for o in orders if o.get("products")]
    removed = [
        (i, len(o.get("products") or []))
        for i, o in enumerate(orders)
        if any(isinstance(p, dict) and p.get("removed") for p in o.get("products") or [])
    ]
    statuses = Counter(str(o.get("status")) for o in orders)
    negatives = sum(
        1
        for r in receipts
        for p in r.get("products") or []
        if isinstance(p, dict) and float(p.get("quantity") or 0) < 0
    )
    twice = sum(
        1
        for r in receipts
        if len({p.get("lagerId") for p in r.get("products") or [] if isinstance(p, dict)})
        < len(r.get("products") or [])
    )
    no_catalog = sum(
        1
        for r in receipts
        for p in r.get("products") or []
        if isinstance(p, dict) and p.get("catalogProduct") is None
    )

    lines = [
        f"online: {len(orders)} orders, {len(with_lines)} with lines, statuses={dict(statuses)}",
        f"online: span of delivered lines {span(on)}, removed lines in orders "
        f"(index, line count): {removed[:8]}{'…' if len(removed) > 8 else ''}",
        f"offline: {len(receipts)} receipts, span {span(off)}, {negatives} negative lines, "
        f"{twice} receipts listing a product twice, {no_catalog} lines with no catalogProduct",
        f"same (product, day) from both sources: {cross_source_same_day(on + off)}",
    ]

    per_product = collapse_same_day(on + off)
    once = sum(1 for days in per_product.values() if len(days) == 1)
    lines.append(f"products: {len(per_product)} distinct, {once} bought exactly once")

    windows: list[tuple[str, date | None]] = [("all history", None)]
    windows.append(("last 365 days", today - timedelta(days=365)))
    if (offline_span := span(off)) is not None:
        windows.append(("receipt coverage", offline_span[0]))
    for label, since in windows:
        found = habits(per_product, since=since)
        cuts = ", ".join(f"CV≤{c}: {count_under(found, c)}" for c in CV_CUTS)
        lines.append(f"habits ({label}): {len(found)} with ≥{MIN_EVENTS} days — {cuts}")
        for h in found[:12]:
            cv = "n/a" if h.cv is None else f"{h.cv:.2f}"
            lines.append(
                f"    {h.key[:12]}…  days={h.events}  median_gap={h.median_gap:.0f}d  cv={cv}"
            )
    return "\n".join(lines)
