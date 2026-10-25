"""Rhythm: what completed history repeats. Pure function of one pull.

Completed records only. A pending bill is not history.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .units import NAME_FIELD, record_key

STEADY_FLOOR_CENTS = 100
STEADY_PCT = Decimal("0.02")


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return int((Decimal(ordered[mid - 1] + ordered[mid]) / 2).quantize(Decimal("1"), ROUND_HALF_UP))


def _status(count: int, median: int | None, low: int | None, high: int | None) -> str:
    if count < 2 or median is None:
        return "sketch"
    allowed = max(Decimal(STEADY_FLOOR_CENTS), Decimal(median) * STEADY_PCT)
    return "steady" if Decimal(high - low) <= allowed else "uneven"


def _one(key: str, records: list[dict[str, Any]], name_field: str) -> dict[str, Any]:
    records = sorted(records, key=lambda r: (r["date"] or date.min, r["id"]))
    amounts = [r["amount_cents"] for r in records]
    dates = [r["date"] for r in records]
    count = len(records)
    median = low = high = None
    if count >= 2:
        median, low, high = _median(amounts), min(amounts), max(amounts)
    known = [d for d in dates if d is not None]
    gaps = [(b - a).days for a, b in zip(known, known[1:])]
    days = {d.day for d in known}
    day_of_month = days.pop() if known and len(known) == count and len(days) == 1 else None
    return {
        "key": key,
        "display": records[-1].get(name_field),
        "completed_count": count,
        "median_cents": median,
        "low_cents": low,
        "high_cents": high,
        "previous_cents": amounts[-1] if amounts else None,
        "gaps_days": gaps,
        "day_of_month": day_of_month,
        "status": _status(count, median, low, high),
        "dates": [d.isoformat() for d in known],
        "last_date": known[-1].isoformat() if known else None,
    }


def build_group(kind: str, records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Rhythm per key for one record kind ("bills", "deposits", "purchases")."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        if r.get("status") != "completed" or r.get("amount_cents") is None:
            continue
        key = record_key(kind, r)
        if key:
            groups.setdefault(key, []).append(r)
    return {key: _one(key, recs, NAME_FIELD[kind]) for key, recs in groups.items()}


def build_rhythm(snapshot: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    return {kind: build_group(kind, snapshot.get(kind, [])) for kind in NAME_FIELD}
