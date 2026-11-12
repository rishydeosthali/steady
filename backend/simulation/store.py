"""Fictional account and local biller. Integer cents, no live banking writes."""
from __future__ import annotations

import calendar
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from ..nessie.client import CustomerNotFound, NessieNotFound
from ..payments.client import BillerError
from ..payments.money import cents


def dollars(value):
    return str(Decimal(value) / 100)


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise ValueError("Use a name between 1 and 80 characters")
    if any(ord(c) < 32 for c in value):
        raise ValueError("Names cannot contain control characters")
    return value.strip()


def positive(value):
    amount = cents(value)
    if not 0 < amount <= 100000000:
        raise ValueError("Use an amount between $0.01 and $1,000,000.00")
    return amount


def iso(value):
    if not isinstance(value, str):
        raise ValueError("Use a date in YYYY-MM-DD format")
    return date.fromisoformat(value).isoformat()


def seed(today, name="Margaret", template="comfortable"):
    run = uuid.uuid4().hex
    ident = lambda: run + "-" + uuid.uuid4().hex[:12]
    today = date.fromisoformat(today)

    def ago(months):
        index = today.year * 12 + today.month - 1 - months
        year, month = divmod(index, 12)
        return date(year, month + 1, min(today.day, calendar.monthrange(year, month + 1)[1])).isoformat()

    state = {"run_id": run, "today": today.isoformat(), "name": name,
             "account_id": ident(), "balance_cents": 221300 if template == "comfortable" else 40000,
             "purchases": [], "deposits": [], "history": [], "invoices": [], "receipts": {},
             "events": [], "applied": {}}
    for n in (3, 2, 1):
        state["deposits"].append({"_id": ident(), "amount": "1874.00", "description": "Social Security",
                                  "transaction_date": ago(n), "status": "completed"})
    state["deposits"].append({"_id": ident(), "amount": "1874.00", "description": "Social Security",
                              "transaction_date": (today + timedelta(days=2)).isoformat(), "status": "pending"})
    for payee, amount, days in (("Austin Energy",12000,3),("Water Co",4525,5),("Spectrum",8999,7)):
        state["invoices"].append({"id": ident(), "customer_ref": "demo-margaret", "payee": payee,
                                  "amount_cents": amount, "currency": "USD", "version": 1,
                                  "due_date": (today + timedelta(days=days)).isoformat(), "status": "unpaid"})
        for n in (2, 1):
            state["history"].append({"_id": ident(), "payee": payee, "payment_amount": dollars(amount),
                                     "payment_date": ago(n), "status": "completed"})
    # History is already reflected in the opening balance.
    for merchant, amount, when in (("Dentist", "120.00", today.isoformat()),
                                   ("StreamBox", "14.99", (today-timedelta(days=30)).isoformat())):
        state["purchases"].append({"_id": ident(), "merchant_id": merchant, "description": merchant,
                                   "amount": amount, "purchase_date": when, "status": "completed"})
    return state


class Store:
    def __init__(self, database, today, name):
        self.database = str(database)
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE IF NOT EXISTS simulation(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO simulation VALUES(1,?)", (json.dumps(seed(today, name)),))
            db.execute("CREATE TABLE IF NOT EXISTS saved_scenarios(id TEXT PRIMARY KEY, name TEXT NOT NULL, body TEXT NOT NULL)")

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.database, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT body FROM simulation WHERE id=1").fetchone()[0])
            yield state
            db.execute("UPDATE simulation SET body=? WHERE id=1", (json.dumps(state),))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def state(self):
        with self.tx() as state:
            return json.loads(json.dumps(state))

    def event(self, state, text):
        state["events"].append({"id": uuid.uuid4().hex, "date": state["today"], "text": text})


class SimulatedBank:
    def __init__(self, store):
        self.store = store

    def resolve(self, first_name):
        state = self.store.state()
        if first_name.casefold() != state["name"].casefold():
            raise CustomerNotFound(first_name, [state["name"]])
        return {"_id": state["account_id"], "type": "Checking"}

    def get_balance(self, account_id):
        return dollars(self.store.state()["balance_cents"])

    def get_bills(self, account_id):
        state = self.store.state()
        return state["history"] + [{"_id": b["id"], "payee": b["payee"],
            "payment_amount": dollars(b["amount_cents"]), "payment_date": b["due_date"],
            "status": "completed" if b["status"] == "paid" else "pending"} for b in state["invoices"]]

    def get_deposits(self, account_id):
        return self.store.state()["deposits"]

    def get_purchases(self, account_id):
        return self.store.state()["purchases"]

    def get_merchant(self, merchant_id):
        return {"_id": merchant_id, "name": merchant_id}

    def create_bill(self, account_id, bill):
        invoice = SimulatedBiller(self.store).create_invoice("demo-margaret", bill["payee"],
                    positive(bill["payment_amount"]), iso(bill["payment_date"]))
        return {"_id": invoice["id"], **bill}

    def update_bill(self, bill_id, bill):
        old = next((b for b in self.get_bills("") if b["_id"] == bill_id), None)
        if old is None:
            raise NessieNotFound("Simulated bill not found")
        return self.create_bill("", {**old, **bill})


class SimulatedBiller:
    """Durable local biller, distinct from the existing independent HTTP provider."""
    def __init__(self, store):
        self.store = store

    def invoices(self, customer):
        return [b for b in self.store.state()["invoices"] if b["customer_ref"] == customer]

    def create_invoice(self, customer, payee, amount_cents, due_date):
        payee, due_date = label(payee), iso(due_date)
        if type(amount_cents) is not int or not 0 < amount_cents <= 100000000:
            raise ValueError("Invalid bill amount")
        with self.store.tx() as state:
            row = next((b for b in state["invoices"] if b["customer_ref"] == customer
                        and b["payee"].casefold() == payee.casefold() and b["status"] == "unpaid"), None)
            if row:
                if (row["amount_cents"], row["due_date"]) != (amount_cents, due_date):
                    row.update(amount_cents=amount_cents, due_date=due_date, version=row["version"] + 1)
            else:
                row = {"id": state["run_id"] + "-" + uuid.uuid4().hex[:12], "customer_ref": customer,
                       "payee": payee, "amount_cents": amount_cents, "currency": "USD", "version": 1,
                       "due_date": due_date, "status": "unpaid"}
                state["invoices"].append(row)
            return dict(row)

    def receipt(self, key):
        return self.store.state()["receipts"].get(key)

    def pay(self, key, payload):
        with self.store.tx() as state:
            prior = state["receipts"].get(key)
            if prior:
                if any(prior.get(k) != v for k, v in payload.items()):
                    raise BillerError("Idempotency key conflicts with prior payment")
                return prior
            bill = next((b for b in state["invoices"] if b["id"] == payload.get("invoice_id")), None)
            if not bill or bill["status"] != "unpaid":
                raise BillerError("Invoice unavailable")
            for k, field in (("customer_ref","customer_ref"),("amount_cents","amount_cents"),
                             ("currency","currency"),("invoice_version","version"),("due_date","due_date")):
                if payload.get(k) != bill[field]:
                    raise BillerError("Invoice changed")
            if payload.get("simulation_date") != state["today"] or state["today"] > bill["due_date"]:
                raise BillerError("Payment date changed")
            receipt = {**payload, "id": "SIM-" + uuid.uuid4().hex[:12].upper(), "payee": bill["payee"],
                       "idempotency_key": key, "status": "accepted", "simulation": True}
            bill["status"] = "paid"
            state["receipts"][key] = receipt
            return receipt
