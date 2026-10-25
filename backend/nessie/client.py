"""Nessie reads. GET only.

Plain functions the agent loop calls. The model never sees a URL and never
picks one. Ids are discovered at runtime; nothing is hardcoded.

Errors are sanitized: a ``requests`` exception message contains the full URL,
which includes the API key, so it is never propagated or logged.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)


class NessieError(Exception):
    """A Nessie read failed. The message never contains the URL or key."""


class CustomerNotFound(NessieError):
    """No customer with the requested name. ``existing`` lists names that exist."""

    def __init__(self, wanted: str, existing: list[str]):
        self.wanted = wanted
        self.existing = existing
        shown = ", ".join(existing) if existing else "(no customers)"
        super().__init__(f"No Nessie customer named {wanted}. Customers that exist: {shown}")


class NessieNotFound(NessieError):
    """Nessie answered 404 for a path."""


class NessieClient:
    def __init__(self, base_url: str, api_key: str, *, session: requests.Session | None = None,
                 timeout: float = 10.0):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._session = session or requests.Session()
        self._timeout = timeout
        self._customer_id: str | None = None
        self._checking: dict[str, Any] | None = None

    # -- transport -------------------------------------------------------

    def _get(self, path: str) -> Any:
        try:
            resp = self._session.get(f"{self._base}{path}", params={"key": self._key},
                                     timeout=self._timeout)
        except requests.RequestException as exc:
            raise NessieError(f"GET {path} failed: {type(exc).__name__}") from None
        if resp.status_code == 404:
            raise NessieNotFound(f"GET {path} returned 404")
        if not resp.ok:
            raise NessieError(f"GET {path} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise NessieError(f"GET {path} returned non-JSON") from None

    # -- discovery -------------------------------------------------------

    def find_customer(self, first_name: str) -> dict[str, Any]:
        """GET /customers and select the customer whose first name matches.

        Raises ``CustomerNotFound`` (with the names that do exist) otherwise.
        Never creates a customer.
        """
        customers = self._get("/customers")
        if not isinstance(customers, list):
            raise NessieError("GET /customers returned an unexpected shape")
        wanted = first_name.strip().lower()
        for c in customers:
            if str(c.get("first_name", "")).strip().lower() == wanted:
                return c
        names = [f"{c.get('first_name', '?')} {c.get('last_name', '')}".strip() for c in customers]
        raise CustomerNotFound(first_name, names)

    def find_checking_account(self, customer_id: str) -> dict[str, Any]:
        """GET /customers/{id}/accounts and return the checking account."""
        accounts = self._get(f"/customers/{customer_id}/accounts")
        if not isinstance(accounts, list):
            raise NessieError("accounts response had an unexpected shape")
        for a in accounts:
            if str(a.get("type", "")).strip().lower() == "checking":
                return a
        raise NessieError("Customer has no checking account")

    def resolve(self, first_name: str) -> dict[str, Any]:
        """Resolve (and cache) the customer's checking account for this client."""
        if self._checking is None:
            customer = self.find_customer(first_name)
            self._customer_id = customer["_id"]
            self._checking = self.find_checking_account(self._customer_id)
        return self._checking

    # -- reads (all take the checking account id) ------------------------

    def get_balance(self, account_id: str) -> Any:
        """Current balance, read fresh from GET /accounts/{id}."""
        account = self._get(f"/accounts/{account_id}")
        return account.get("balance") if isinstance(account, dict) else None

    def get_bills(self, account_id: str) -> list[dict[str, Any]]:
        return self._list(f"/accounts/{account_id}/bills")

    def get_deposits(self, account_id: str) -> list[dict[str, Any]]:
        return self._list(f"/accounts/{account_id}/deposits")

    def get_purchases(self, account_id: str) -> list[dict[str, Any]]:
        return self._list(f"/accounts/{account_id}/purchases")

    def get_merchant(self, merchant_id: str) -> dict[str, Any]:
        """A purchase carries only a merchant id; the name lives here."""
        data = self._get(f"/merchants/{merchant_id}")
        if not isinstance(data, dict):
            raise NessieError("merchant response had an unexpected shape")
        return data

    def _list(self, path: str) -> list[dict[str, Any]]:
        try:
            data = self._get(path)
        except NessieNotFound:
            # Nessie answers 404 for an account with none of these. That is a
            # real "nothing there", not an outage.
            return []
        if not isinstance(data, list):
            raise NessieError(f"GET {path} returned an unexpected shape")
        return data
