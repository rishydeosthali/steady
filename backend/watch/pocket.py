"""Pocket: the $200 buffer plus bills due on or before the next deposit.

Pure. Computed for each judgment and never stored as a guess. Uses the
balance Nessie returns as is: the sandbox does not lower it when purchases
post, so nothing here subtracts history from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .units import BUFFER_CENTS, WINDOW_DAYS, days_after


@dataclass(frozen=True)
class Pocket:
    balance_cents: int
    purchase_cents: int
    reserved: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    reserved_cents: int = 0
    left_after_cents: int = 0
    horizon_date: date | None = None
    horizon_reason: str = "next_deposit"
    horizon_label: str | None = None

    @property
    def holds(self) -> bool:
        """Balance after the purchase and reserved bills stays above the buffer."""
        return self.left_after_cents > BUFFER_CENTS


def compute_pocket(balance_cents: int | None, bills: list[dict[str, Any]],
                   deposits: list[dict[str, Any]], clock: date, purchase_cents: int,
                   deposit_missing: bool) -> Pocket | None:
    """Judge one purchase against the pocket. None when the balance is unknown."""
    if balance_cents is None:
        return None

    if deposit_missing:
        horizon, reason, label = days_after(clock, WINDOW_DAYS), "deposit_missing", None
    else:
        upcoming = sorted((d for d in deposits
                           if d.get("status") == "pending" and d.get("date") and d["date"] >= clock),
                          key=lambda d: (d["date"], d["id"]))
        if upcoming:
            horizon, reason, label = upcoming[0]["date"], "next_deposit", upcoming[0].get("description")
        else:
            horizon, reason, label = days_after(clock, WINDOW_DAYS), "no_deposit_on_file", None

    # Due on the deposit day is reserved: never assume the deposit posts first.
    # An undated pending bill is reserved too; unknown is not the same as later.
    reserved = tuple(sorted(
        (b for b in bills
         if b.get("status") == "pending" and b.get("amount_cents") is not None
         and (b.get("date") is None or b["date"] <= horizon)),
        key=lambda b: (b.get("date") or date.max, b["id"])))
    reserved_cents = sum(b["amount_cents"] for b in reserved)
    return Pocket(
        balance_cents=balance_cents,
        purchase_cents=purchase_cents,
        reserved=reserved,
        reserved_cents=reserved_cents,
        left_after_cents=balance_cents - purchase_cents - reserved_cents,
        horizon_date=horizon,
        horizon_reason=reason,
        horizon_label=label,
    )
