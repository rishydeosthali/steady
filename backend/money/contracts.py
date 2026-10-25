"""Function boundaries for later slices. NOT IMPLEMENTED, NOT CALLED.

These are the "Agent to backend" calls from docs/FEATURES_AND_GOALS.md. They
exist so later slices have a place to land. The WhatsApp base agent never
imports or calls this module: no payment can be created, cancelled, or
approved in this slice.
"""

from __future__ import annotations

from typing import Any

_MSG = "Not available in the WhatsApp base slice: this money action is not turned on."


def get_upcoming_bills(account_id: str, window_days: int) -> list[dict[str, Any]]:
    raise NotImplementedError(_MSG)


def propose_date(bill: dict[str, Any], buffer_dollars: float = 200) -> dict[str, Any]:
    raise NotImplementedError(_MSG)


def request_payment(payee: str, amount: float, date: str) -> dict[str, Any]:
    """Should return allow / needs Sarah / blocked, each with a reason."""
    raise NotImplementedError(_MSG)


def execute(payment_request_id: str) -> dict[str, Any]:
    raise NotImplementedError(_MSG)


def cancel(payment_id: str) -> dict[str, Any]:
    raise NotImplementedError(_MSG)


def verify_claim(payee: str) -> dict[str, Any]:
    raise NotImplementedError(_MSG)
