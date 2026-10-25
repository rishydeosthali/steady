"""Deterministic, fictional Nessie read snapshots and future test expectations.

Amounts in the authored files are integer cents. Dollars exist only at the
Nessie read boundary. This is a data factory, not a planner or payment engine.
"""

from __future__ import annotations

import calendar
import json
import re
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "synthetic"


def _read(name: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def _cents(value: Any, label: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer number of cents")


def _day(value: Any, label: str) -> None:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer day offset")


def validate_catalog() -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate authored inputs, including references, before materializing them."""
    profiles, scenarios = _read("profiles.json"), _read("scenarios.json")
    if profiles.get("schema_version") != 1 or scenarios.get("schema_version") != 1:
        raise ValueError("Unsupported synthetic data schema version")
    if profiles.get("synthetic") is not True or profiles.get("currency") != "USD":
        raise ValueError("Only explicitly synthetic USD data is supported")
    date.fromisoformat(profiles["anchor_date"])

    def indexed(rows: list[dict], label: str) -> dict[str, dict]:
        result = {}
        for row in rows:
            key = row["id"]
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key):
                raise ValueError(f"Invalid {label} id: {key}")
            if key in result:
                raise ValueError(f"Duplicate {label} id: {key}")
            result[key] = row
        return result

    by_profile = indexed(profiles["profiles"], "profile")
    indexed(scenarios["scenarios"], "scenario")

    def check_bills(rows: list[dict]) -> None:
        indexed(rows, "bill")
        for bill in rows:
            _cents(bill["amount_cents"], "bill amount", nullable=True)
            _day(bill["due_day"], "bill due day")
            if bill["due_day"] < 0 or not bill.get("payee"):
                raise ValueError("Upcoming bills need a payee and a nonnegative due day")

    for profile in by_profile.values():
        for field in ("balance_cents", "buffer_cents", "family_threshold_cents"):
            _cents(profile[field], field)
        _cents(profile["income"]["amount_cents"], "income")
        _day(profile["income"]["next_day"], "income day")
        if not 0 < profile["income"]["next_day"] <= 28:
            raise ValueError("Expected income must be within the next 28 days")
        check_bills(profile["bills"])
        for purchase in profile["weekly_purchases"] + profile["subscriptions"]:
            _cents(purchase["amount_cents"], "purchase")

    for case in scenarios["scenarios"]:
        if case["profile"] not in by_profile:
            raise ValueError(f"Unknown profile in {case['id']}")
        profile = by_profile[case["profile"]]
        if "balance_cents" in case:
            _cents(case["balance_cents"], "scenario balance")
        bills = case.get("bills", profile["bills"])
        check_bills(bills)
        if "target_bill" in case and case["target_bill"] not in {b["id"] for b in bills}:
            raise ValueError(f"Unknown target bill in {case['id']}")
        if "claimed_bill" in case:
            possible = {f"{b['id']}-history-{i}" for b in profile["bills"] for i in (1, 2)}
            if case["claimed_bill"] not in possible:
                raise ValueError(f"Unknown historical bill in {case['id']}")
        for purchase in case.get("extra_purchases", []):
            _cents(purchase["amount_cents"], "extra purchase")
            _day(purchase["day"], "extra purchase day")
            if purchase["day"] > 0:
                raise ValueError("Extra settled purchases cannot be in the future")
        for field in ("proposed_purchase", "payment_request"):
            if field in case:
                _cents(case[field]["amount_cents"], field)
                _day(case[field]["day"], field + " day")
        for field in ("scheduled_day", "cancel_day"):
            if field in case:
                _day(case[field], field)
        if "income_delay_days" in case:
            _day(case["income_delay_days"], "income delay")
            if case["income_delay_days"] < 0:
                raise ValueError("Income delay cannot be negative")
        if "cancel_day" in case and case["cancel_day"] >= case["scheduled_day"]:
            raise ValueError("Cancellation case must precede its scheduled payment")
        if "expected" not in case or not case.get("roles"):
            raise ValueError("Scenarios must state their expectations and owning roles")
    return profiles, scenarios


def list_scenarios() -> list[dict[str, Any]]:
    return validate_catalog()[1]["scenarios"]


def _months_before(day: date, count: int) -> date:
    month_index = day.year * 12 + day.month - 1 - count
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _dollars(cents: int | None) -> float | None:
    return None if cents is None else float(Decimal(cents) / Decimal(100))


def load_scenario(scenario_id: str = "safe_payment", *, as_of: date | None = None) -> dict[str, Any]:
    """Return a new isolated snapshot; resetting is simply calling this again.

    ``as_of`` rebases all records, histories, and expectation dates together.
    Historical transactions are already reflected in the opening balance.
    Expected income is pending, not an immediately available balance credit.
    """
    catalog, scenarios = validate_catalog()
    try:
        case = next(c for c in scenarios["scenarios"] if c["id"] == scenario_id)
    except StopIteration:
        raise ValueError(f"Unknown scenario: {scenario_id}. Use the list command.") from None
    profile = deepcopy(next(p for p in catalog["profiles"] if p["id"] == case["profile"]))
    anchor = as_of or date.fromisoformat(catalog["anchor_date"])
    if type(anchor) is not date:
        raise ValueError("as_of must be a datetime.date")

    def day(offset: int) -> str:
        return (anchor + timedelta(days=offset)).isoformat()

    def ident(kind: str, name: str = "") -> str:
        return f"synthetic-{profile['id']}-{kind}" + (f"-{name}" if name else "")

    account_id, customer_id = ident("checking"), ident("customer")
    customer = {"_id": customer_id, "first_name": profile["first_name"], "last_name": "Demo"}
    account = {"_id": account_id, "customer_id": customer_id, "type": "Checking",
               "nickname": "SYNTHETIC Everyday Checking", "rewards": 0,
               "balance": _dollars(case.get("balance_cents", profile["balance_cents"]))}
    bills, bill_metadata = [], {}

    def bill_record(bill: dict, local_id: str, when: str, status: str) -> dict:
        return {"_id": ident("bill", local_id), "account_id": account_id,
                "payee": bill["payee"], "nickname": bill["nickname"],
                "payment_amount": _dollars(bill["amount_cents"]),
                "payment_date": when, "status": status}

    # Two paid observations per known bill, plus all upcoming obligations.
    for bill in profile["bills"]:
        due = anchor + timedelta(days=bill["due_day"])
        for month in (2, 1):
            local_id = f"{bill['id']}-history-{month}"
            bills.append(bill_record(bill, local_id, _months_before(due, month).isoformat(), "completed"))
    for bill in case.get("bills", profile["bills"]):
        bills.append(bill_record(bill, bill["id"], day(bill["due_day"]), "pending"))
        bill_metadata[ident("bill", bill["id"])] = {"due_date": day(bill["due_day"]),
                                                    "user_approved": False}

    income = profile["income"]
    next_income = anchor + timedelta(days=income["next_day"])
    deposits = [{"_id": ident("deposit", str(month)), "payee_id": account_id,
                 "medium": "balance", "type": "deposit", "amount": _dollars(income["amount_cents"]),
                 "description": income["description"], "status": "completed" if month else "pending",
                 "transaction_date": _months_before(next_income, month).isoformat()}
                for month in (3, 2, 1, 0)]
    if case.get("income_delay_days"):
        deposits[-1]["transaction_date"] = day(income["next_day"] + case["income_delay_days"])

    purchases, merchants = [], {}

    def purchase_record(item: dict, when: str) -> None:
        merchant = item["merchant"]
        if merchant not in merchants:
            merchants[merchant] = {"_id": ident("merchant", str(len(merchants) + 1)), "name": merchant}
        purchases.append({"_id": ident("purchase", str(len(purchases) + 1)),
                          "payer_id": account_id, "merchant_id": merchants[merchant]["_id"],
                          "medium": "balance", "type": "merchant", "status": "completed",
                          "description": item.get("description", "Monthly subscription"),
                          "purchase_date": when, "amount": _dollars(item["amount_cents"])})

    for week in range(12, 0, -1):
        for i, item in enumerate(profile["weekly_purchases"]):
            purchase_record(item, day(-7 * week + i))
    for item in profile["subscriptions"]:
        for month in (3, 2, 1):
            purchase_record(item, _months_before(anchor - timedelta(days=1), month).isoformat())
    for item in case.get("extra_purchases", []):
        purchase_record(item, day(item["day"]))

    expected = deepcopy(case["expected"])
    if "earliest_safe_day" in expected:
        offset = expected.pop("earliest_safe_day")
        expected["earliest_safe_date"] = None if offset is None else day(offset)
    context = {"profile_id": profile["id"], "age_band": profile["age_band"],
               "trusted_person": profile["trusted_person"], "buffer_cents": profile["buffer_cents"],
               "family_threshold_cents": profile["family_threshold_cents"],
               "known_payees": [b["payee"] for b in profile["bills"]],
               "bill_metadata": bill_metadata}
    for field in ("target_bill", "claimed_bill"):
        if field in case:
            context[field + "_id"] = ident("bill", case[field])
    for field in ("proposed_purchase", "payment_request", "forwarded_claim"):
        if field in case:
            context[field] = deepcopy(case[field])
            if "day" in context[field]:
                context[field]["date"] = day(context[field].pop("day"))
    for field in ("scheduled_day", "cancel_day"):
        if field in case:
            context[field.replace("_day", "_date")] = day(case[field])

    return {"schema_version": 1, "synthetic": True, "scenario_id": case["id"],
            "title": case["title"], "as_of": anchor.isoformat(), "currency": "USD",
            "customer": customer, "account": account, "bills": bills,
            "deposits": deposits, "purchases": purchases, "merchants": list(merchants.values()),
            "context": context, "messages": deepcopy(case["messages"]),
            "expected": expected, "notes": case["notes"]}
