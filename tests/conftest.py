"""Shared fakes. Every value here is a dummy; no real credential is used."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import Settings  # noqa: E402
from backend.nessie.client import NessieNotFound  # noqa: E402

DUMMY_TOKEN = "dummy-auth-token"
DUMMY_NESSIE_KEY = "dummy-nessie-key"
MOM = "whatsapp:+15550001111"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        twilio_account_sid="ACdummy",
        twilio_auth_token=DUMMY_TOKEN,
        twilio_whatsapp_from="whatsapp:+15550002222",
        mom_whatsapp_to=MOM,
        twilio_content_sid="HXdummy",
        nessie_api_key=DUMMY_NESSIE_KEY,
        nessie_base_url="https://nessie.invalid",
        openai_api_key="dummy-openai-key",
    )


class FakeLLM:
    """Returns scripted JSON per schema name and records every call."""

    model = "fake"

    def __init__(self, intent: dict | None = None, reply: dict | None = None):
        self.intent = intent
        self.reply = reply
        self.calls: list[tuple[str, str, str]] = []   # (schema, system, user)

    def json_completion(self, system, user, schema_name, schema):
        self.calls.append((schema_name, system, user))
        data = self.intent if schema_name == "steady_intent" else self.reply
        if data is None:
            raise AssertionError(f"unexpected LLM call: {schema_name}")
        return data


class FakeNessie:
    """Read-only stand-in with the same read methods. Records every call."""

    def __init__(self, balance=1500.5, bills=None, deposits=None, purchases=None,
                 customers=("Margaret",)):
        self.balance = balance
        self.bills = bills if bills is not None else [
            {"payee": "Austin Energy", "payment_amount": 120.0, "payment_date": "2026-10-31",
             "status": "pending"},
            {"payee": "Water Co", "payment_amount": 45.25, "payment_date": "2026-11-03",
             "status": "pending"},
        ]
        self.deposits = deposits or []
        self.purchases = purchases or []
        self.customers = customers
        self.calls: list[str] = []

    def resolve(self, first_name):
        self.calls.append("resolve")
        if first_name not in self.customers:
            from backend.nessie.client import CustomerNotFound
            raise CustomerNotFound(first_name, list(self.customers))
        return {"_id": "acct-1", "type": "Checking"}

    def get_balance(self, account_id):
        self.calls.append("balance")
        return self.balance

    def get_bills(self, account_id):
        self.calls.append("bills")
        return self.bills

    def get_deposits(self, account_id):
        self.calls.append("deposits")
        return self.deposits

    def get_purchases(self, account_id):
        self.calls.append("purchases")
        return self.purchases


class FakeResponse:
    def __init__(self, status: int, payload: Any):
        self.status_code = status
        self._payload = payload
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


class FakeSession:
    """requests.Session stand-in keyed by path. Records method and path."""

    def __init__(self, routes: dict[str, FakeResponse]):
        self.routes = routes
        self.requests: list[tuple[str, str, dict]] = []

    def get(self, url, params=None, timeout=None):
        path = url.replace("https://nessie.invalid", "")
        self.requests.append(("GET", path, params or {}))
        return self.routes.get(path, FakeResponse(404, {}))
