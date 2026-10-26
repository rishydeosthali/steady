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

    def __init__(self, intent: dict | None = None, reply: dict | None = None,
                 tools: dict | None = None, photo: dict | None = None,
                 speaker: dict | None = None):
        self.intent = intent
        self.reply = reply
        self.tools = tools
        self.photo = photo
        self.speaker = speaker
        self.calls: list[tuple[str, str, str]] = []   # (schema, system, user)
        self.images: list[tuple[str, bytes]] = []

    def json_completion(self, system, user, schema_name, schema):
        self.calls.append((schema_name, system, user))
        if schema_name == "steady_speaker":
            # Existing tests do not script this. Default: it is Margaret.
            return self.speaker if self.speaker is not None else {
                "speaker": "margaret", "confidence": 0.95, "flags": [],
            }
        if schema_name == "steady_intent":
            data = self.intent
        elif schema_name == "steady_tools":
            if self.tools is None:
                from backend.agent.llm import LLMError
                raise LLMError("no tools scripted")
            data = self.tools
        elif schema_name == "steady_bill_photo":
            data = self.photo
        else:
            data = self.reply
        if data is None:
            raise AssertionError(f"unexpected LLM call: {schema_name}")
        return data

    def json_vision(self, system, image, mime, schema_name, schema):
        self.images.append((mime, image))
        return self.json_completion(system, f"[image {mime} {len(image)} bytes]", schema_name, schema)


class FakeNessie:
    """Read-only stand-in with the same read methods. Records every call."""

    def __init__(self, balance=1500.5, bills=None, deposits=None, purchases=None,
                 customers=("Margaret",)):
        self.balance = balance
        self.bills = bills if bills is not None else [
            {"_id": "bill-ae", "payee": "Austin Energy", "nickname": "Electric",
             "payment_amount": 120.0, "payment_date": "2026-10-31", "status": "pending"},
            {"_id": "bill-water", "payee": "Water Co", "nickname": "Water",
             "payment_amount": 45.25, "payment_date": "2026-11-03", "status": "pending"},
        ]
        self.writes: list[tuple[str, Any]] = []
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

    def create_bill(self, account_id, bill):
        self.calls.append("create_bill")
        self.writes.append(("create", dict(bill)))
        created = {"_id": f"bill-{len(self.bills) + 1}", **bill}
        self.bills.append(created)
        return created

    def update_bill(self, bill_id, bill):
        self.calls.append("update_bill")
        self.writes.append(("update", bill_id, dict(bill)))
        for i, existing in enumerate(self.bills):
            if existing.get("_id") == bill_id:
                updated = {**existing, **bill, "_id": bill_id}
                self.bills[i] = updated
                return updated
        raise NessieNotFound(f"PUT /bills/{bill_id} returned 404")


class FakeResponse:
    def __init__(self, status: int, payload: Any):
        self.status_code = status
        self._payload = payload
        self.ok = 200 <= status < 300
        self.content = b"{}" if payload is not None else b""

    def json(self):
        return self._payload


class FakeSession:
    """requests.Session stand-in keyed by path. Records method and path."""

    def __init__(self, routes: dict[str, FakeResponse]):
        self.routes = routes
        self.requests: list[tuple[str, str, dict]] = []
        self.bodies: list[Any] = []

    def get(self, url, params=None, timeout=None):
        return self.request("GET", url, params=params, timeout=timeout)

    def request(self, method, url, params=None, json=None, timeout=None):
        path = url.replace("https://nessie.invalid", "")
        self.requests.append((method.upper(), path, params or {}))
        self.bodies.append(json)
        return self.routes.get(path, FakeResponse(404, {}))
