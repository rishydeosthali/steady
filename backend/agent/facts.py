"""Turn Nessie reads into the only facts a reply may use.

Code formats every amount and date. The reply writer copies them. The writer
never computes, rounds, or infers a money value.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from ..nessie.client import NessieClient, NessieError

log = logging.getLogger(__name__)

MAX_ITEMS = 10


def money(value: Any) -> str | None:
    """'$1,234.56' from a Nessie number, or None when unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return None


def display_date(value: Any) -> str | None:
    """'Oct 31' from a YYYY-MM-DD (or longer ISO) string, or None when unknown."""
    if not value:
        return None
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return f"{d.strftime('%b')} {d.day}"


def _bills(raw: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for b in sorted(raw, key=lambda b: str(b.get("payment_date") or b.get("upcoming_payment_date")
                                           or "9999-99-99")):
        when = b.get("payment_date") or b.get("upcoming_payment_date")
        items.append({
            "payee": b.get("payee") or None,
            "amount": money(b.get("payment_amount")),
            "payment_date": display_date(when),
            "status": b.get("status") or None,
        })
    return {"status": "ok", "count": len(items), "items": items[:MAX_ITEMS]}


def _deposits(raw: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(raw, key=lambda d: str(d.get("transaction_date") or ""), reverse=True)
    items = [{
        "description": d.get("description") or d.get("type") or None,
        "amount": money(d.get("amount")),
        "date": display_date(d.get("transaction_date")),
        "status": d.get("status") or None,
    } for d in ordered]
    return {"status": "ok", "count": len(items), "items": items[:MAX_ITEMS]}


def _purchases(raw: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(raw, key=lambda p: str(p.get("purchase_date") or ""), reverse=True)
    items = [{
        "description": p.get("description") or None,
        "amount": money(p.get("amount")),
        "date": display_date(p.get("purchase_date")),
    } for p in ordered]
    return {"status": "ok", "count": len(items), "items": items[:MAX_ITEMS]}


def gather_facts(nessie: NessieClient, account_id: str, needs: tuple[str, ...]) -> dict[str, Any]:
    """Read only what the question needs. A failed read becomes ``unavailable``."""
    facts: dict[str, Any] = {}
    for need in needs:
        try:
            if need == "balance":
                amount = money(nessie.get_balance(account_id))
                facts["balance"] = ({"status": "ok", "amount": amount} if amount
                                    else {"status": "unavailable"})
            elif need == "bills":
                facts["bills"] = _bills(nessie.get_bills(account_id))
            elif need == "deposits":
                facts["deposits"] = _deposits(nessie.get_deposits(account_id))
            elif need == "purchases":
                facts["purchases"] = _purchases(nessie.get_purchases(account_id))
        except NessieError as exc:
            log.warning("Nessie read failed for %s: %s", need, exc)
            facts[need] = {"status": "unavailable"}
    return facts
