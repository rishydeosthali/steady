"""Fixtures for the watcher, labeled ``source: fixture``.

    python -m backend.watch.demo

Fixtures never touch live Nessie. Tests import them from here.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from .judge import diff, frame_of, judge, judge_contemplated
from .rhythm import build_rhythm
from .units import parse_date, to_cents
from .wording import fallback

LIVE_CLOCK = date(2026, 10, 24)
THURSDAY_CLOCK = date(2026, 10, 26)


def bill(id_: str, payee: str, amount: str, when: str, status: str = "pending") -> dict[str, Any]:
    return {"id": id_, "payee": payee, "amount_cents": to_cents(amount), "date": parse_date(when),
            "status": status}


def deposit(id_: str, amount: str, when: str, status: str = "completed",
            description: str = "Social Security") -> dict[str, Any]:
    return {"id": id_, "description": description, "amount_cents": to_cents(amount),
            "date": parse_date(when), "status": status}


def purchase(id_: str, merchant: str, amount: str, when: str,
             status: str = "completed") -> dict[str, Any]:
    return {"id": id_, "merchant": merchant, "merchant_id": None, "amount_cents": to_cents(amount),
            "date": parse_date(when), "status": status}


def snapshot(balance: str | None, bills=(), deposits=(), purchases=()) -> dict[str, Any]:
    return {"source": "fixture", "balance_cents": to_cents(balance), "bills": list(bills),
            "deposits": list(deposits), "purchases": list(purchases)}


def live_shape() -> dict[str, Any]:
    """The live Nessie shape, clock 2026-10-24."""
    return snapshot(
        "2213.00",
        bills=[
            bill("b-ae", "Austin Energy", "120.00", "2026-10-31"),
            bill("b-water", "Water Co", "45.25", "2026-11-03"),
            bill("b-spectrum", "Spectrum", "89.99", "2026-11-12"),
            bill("b-ae-sep", "Austin Energy", "118.60", "2026-09-30", "completed"),
        ],
        deposits=[
            deposit("d-aug", "1874.00", "2026-08-03"),
            deposit("d-sep", "1874.00", "2026-09-03"),
            deposit("d-oct", "1874.00", "2026-10-03"),
            deposit("d-nov", "1874.00", "2026-11-03", "pending"),
        ],
        purchases=[
            purchase("p-heb1", "H-E-B", "47.00", "2026-10-06"),
            purchase("p-heb2", "H-E-B", "62.00", "2026-10-18"),
            purchase("p-shell", "Shell", "34.00", "2026-10-11"),
            purchase("p-cvs", "CVS", "18.00", "2026-10-20"),
            purchase("p-walg", "Walgreens", "15.00", "2026-10-22"),
        ],
    )


def thursday_shape() -> dict[str, Any]:
    """Clock Monday 2026-10-26. Austin Energy due Thursday."""
    return snapshot(
        "400.00",
        bills=[bill("b-ae", "Austin Energy", "120.00", "2026-10-29")],
        deposits=[deposit("d-nov", "1874.00", "2026-11-03", "pending")],
    )


def bill_increase_shape(pending: str = "85.00", completed=("65.00", "65.00")) -> dict[str, Any]:
    bills = [bill(f"b-net-{i}", "internet", amt, f"2026-{8 + i:02d}-15", "completed")
             for i, amt in enumerate(completed)]
    bills.append(bill("b-net-pending", "internet", pending, "2026-11-15"))
    return snapshot("2213.00", bills=bills,
                    deposits=[deposit("d-nov", "1874.00", "2026-11-03", "pending")])


def deposit_missing_shape(bills=()) -> dict[str, Any]:
    """Social Security Jul, Aug, Sep 3. Nothing in October. Clock Oct 6."""
    return snapshot(
        "2213.00",
        bills=bills,
        deposits=[deposit("d-jul", "1874.00", "2026-07-03"),
                  deposit("d-aug", "1874.00", "2026-08-03"),
                  deposit("d-sep", "1874.00", "2026-09-03")],
    )


def run_pair(before: dict[str, Any] | None, after: dict[str, Any], clock: date,
             tape=()) -> list[dict[str, Any]]:
    """Judge ``after`` against a frame built from ``before`` (None means first look)."""
    frame = frame_of(before) if before is not None else {}
    return judge(after, build_rhythm(after), diff(frame, after), list(tape), clock,
                 first_look=not frame).verdicts


def _show(title: str, verdicts: list[dict[str, Any]]) -> None:
    print(f"\n== {title} ==")
    for v in verdicts:
        row = {k: v[k] for k in ("kind", "source", "notify", "consumer", "dedupe_key",
                                 "over_threshold")}
        if v["notify"]:
            row["fallback"] = fallback(v)
        row["facts"] = v["facts"]
        print(json.dumps(row, indent=2))
    if not verdicts:
        print("(no verdicts)")


def main() -> int:
    live = live_shape()
    _show("live shape, first look, clock 2026-10-24", run_pair(None, live, LIVE_CLOCK))

    rhythm = build_rhythm(live)
    for amount in ("1900.00", "1847.75", "1847.74"):
        result = judge_contemplated(live, rhythm, [], LIVE_CLOCK, "Dentist", to_cents(amount))
        _show(f"contemplate ${amount} at the dentist", [result.verdict] if result.verdict else [])

    thursday = thursday_shape()
    result = judge_contemplated(thursday, build_rhythm(thursday), [], THURSDAY_CLOCK, "Pharmacy",
                                to_cents("100.00"))
    _show("Thursday shape, contemplate $100", [result.verdict])

    _show("bill increase $65 -> $85", run_pair(None, bill_increase_shape(), LIVE_CLOCK))

    before = snapshot("2213.00", purchases=[purchase("p-d1", "dentist", "120.00", "2026-10-24")])
    after = snapshot("2213.00", purchases=[purchase("p-d1", "dentist", "120.00", "2026-10-24"),
                                           purchase("p-d2", "dentist", "120.00", "2026-10-24")])
    _show("duplicate dentist charges", run_pair(before, after, LIVE_CLOCK))

    before = snapshot("2213.00", purchases=[purchase("p-s1", "StreamBox", "14.99", "2026-09-24")])
    after = snapshot("2213.00", purchases=[purchase("p-s1", "StreamBox", "14.99", "2026-09-24"),
                                           purchase("p-s2", "StreamBox", "14.99", "2026-10-24")])
    _show("StreamBox second charge", run_pair(before, after, LIVE_CLOCK))

    _show("Social Security missing in October, clock Oct 6",
          run_pair(None, deposit_missing_shape(), date(2026, 10, 6)))

    roofer = snapshot("2213.00", bills=[bill("b-roof", "Roofer", "2400.00", "2026-10-30")])
    tape = [{"payee_key": "roofer", "decision": "sarah_denied", "payment_date": None}]
    _show("held Roofer payment reappears", run_pair(None, roofer, LIVE_CLOCK, tape))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
