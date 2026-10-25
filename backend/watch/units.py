"""Pure helpers shared by the watcher: cents, keys, dates, and record shapes.

No I/O. Money is integer cents built with ``Decimal``. Floats never reach the
judge.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

WINDOW_DAYS = 14
BUFFER_CENTS = 20000
THRESHOLD_CENTS = 50000


def to_cents(value: Any) -> int | None:
    """Integer cents from a Nessie number or string. Bools and junk are None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip().replace(",", "").lstrip("$"))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def key_of(name: Any) -> str:
    """Exact-match key: casefold, '&' to 'and', no punctuation, single spaces.

    'H-E-B' and 'HEB' share a key. There is no similarity matching anywhere.
    """
    if not name:
        return ""
    text = str(name).casefold().replace("&", "and")
    text = re.sub(r"[^\w\s]|_", "", text)
    return " ".join(text.split())


def parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def money_full(cents: int | None) -> str | None:
    """'$1,900.00'. Negative amounts read '-$12.00'."""
    if cents is None:
        return None
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rem:02d}"


def money_short(cents: int | None) -> str | None:
    """'$65' for whole dollars, '$14.99' otherwise."""
    if cents is None:
        return None
    if cents % 100 == 0:
        sign = "-" if cents < 0 else ""
        return f"{sign}${abs(cents) // 100:,}"
    return money_full(cents)


def fmt_date(d: date | None) -> str | None:
    """'Oct 31'."""
    return f"{d.strftime('%b')} {d.day}" if d else None


def weekday(d: date | None) -> str | None:
    """'Saturday', from ``date.strftime``."""
    return d.strftime("%A") if d else None


def next_month_on_day(last: date, day: int) -> date:
    """``day`` in the month after ``last``. Short months use their last day."""
    year, month = (last.year + 1, 1) if last.month == 12 else (last.year, last.month + 1)
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def days_after(d: date, n: int) -> date:
    return d + timedelta(days=n)


# -- record shapes ---------------------------------------------------------
# A snapshot is {"source", "balance_cents", "bills", "deposits", "purchases"}.
# Every record carries "id", "amount_cents", "date", "status", plus a name.

def bill_record(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(raw.get("_id")),
        "payee": raw.get("payee") or None,
        "amount_cents": to_cents(raw.get("payment_amount")),
        "date": parse_date(raw.get("payment_date") or raw.get("upcoming_payment_date")),
        "status": str(raw.get("status") or "").lower(),
    }


def deposit_record(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(raw.get("_id")),
        "description": raw.get("description") or None,
        "amount_cents": to_cents(raw.get("amount")),
        "date": parse_date(raw.get("transaction_date")),
        "status": str(raw.get("status") or "").lower(),
    }


def purchase_record(raw: dict[str, Any], merchant_name: str | None) -> dict[str, Any]:
    return {
        "id": str(raw.get("_id")),
        "merchant": merchant_name or None,
        "merchant_id": raw.get("merchant_id") or None,
        "amount_cents": to_cents(raw.get("amount")),
        "date": parse_date(raw.get("purchase_date")),
        "status": str(raw.get("status") or "").lower(),
    }


NAME_FIELD = {"bills": "payee", "deposits": "description", "purchases": "merchant"}


def record_key(kind: str, record: dict[str, Any]) -> str:
    """Key for a record. A purchase with no merchant name keys on its merchant id."""
    key = key_of(record.get(NAME_FIELD[kind]))
    if not key and kind == "purchases" and record.get("merchant_id"):
        return f"merchant id {record['merchant_id']}"
    return key
