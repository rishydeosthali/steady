"""Shared simulation runtime for dashboard, original agent, and scheduler."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..agent.audit import AuditLog
from ..payments.service import PaymentService
from ..payments.money import cents, money
from ..watch.ledger import Ledger
from ..watch.run import Watcher, nessie_puller
from .store import Store, SimulatedBank, SimulatedBiller, dollars, iso, label, positive, seed

log = logging.getLogger(__name__)
PRESETS = [
    {"id":"shortfall", "name":"Unexpected dentist visit", "description":"Post a $1,900 expense and check the bills."},
    {"id":"duplicate", "name":"Charged twice", "description":"Post another $120 dentist charge."},
    {"id":"subscription", "name":"A subscription returns", "description":"Post another $14.99 StreamBox charge."},
    {"id":"bill_increase", "name":"Electric bill goes up", "description":"Change Austin Energy from $120 to $180."},
    {"id":"delay_income", "name":"Income arrives late", "description":"Delay the next deposit by seven days."},
]


class Conflict(ValueError):
    pass


class SimulationPayments(PaymentService):
    def __init__(self, *args, runtime, **kwargs):
        self.runtime = runtime
        super().__init__(*args, **kwargs)

    @contextmanager
    def _tx(self):
        with self.runtime.lock:
            with super()._tx() as pair:
                yield pair

    def today(self, state):
        return self.runtime.store.state()["today"]

    def tick(self):
        # Recheck future approvals immediately after an operator changes the
        # account, rather than waiting until the scheduled day to discover it.
        with self._tx() as (_, state):
            invoices = self._invoices()
            bank = self._bank(state)
            for job in state["jobs"].values():
                if job["status"] not in {"awaiting_user", "awaiting_caregiver", "scheduled"}:
                    continue
                invoice = next((b for b in invoices if b["id"] == job["invoice_id"]), None)
                changed = invoice is None or not self._matches(job, invoice) or invoice["status"] != "unpaid"
                unsafe = not changed and not self._safe(state, invoice, invoices, bank, job["scheduled_for"])
                if changed or unsafe:
                    job["status"] = "held"
                    job["hold_reason"] = "invoice_changed" if changed else "funds"
                    self._close_queued_readbacks(state, job["id"], "superseded")
                    self._audit(state, "simulation_changed_payment_held", job)
                    self._emit(state, f"I put your {job['payee']} payment on hold because "
                               "the bill or your available money changed. Nothing was sent. "
                               "A different amount or date needs a new approval.", job)
        super().tick()
        self.replan_funds()

    def replan_funds(self):
        """A recovered forecast gets a fresh proposal, never inherited consent."""
        with self._tx() as (_, state):
            if any(j["status"] in {"awaiting_user", "awaiting_caregiver", "scheduled", "reconciling"}
                   for j in state["jobs"].values()):
                return
            latest = {j["invoice_id"]:j for j in state["jobs"].values()}
            invoices, bank = self._invoices(), self._bank(state)
            for old in latest.values():
                if old["status"] != "held" or old.get("hold_reason") != "funds":
                    continue
                bill = next((b for b in invoices if b["id"] == old["invoice_id"]), None)
                if not bill or not self._matches(old, bill) or bill["status"] != "unpaid":
                    continue
                if bill["payee"] not in self.known_payees or bill["amount_cents"] > self.max_cents:
                    continue
                when = self._plan(state, bill, invoices, bank)
                if when is None:
                    continue
                ref = uuid.uuid4().hex[:8].upper()
                while ref in state["jobs"]:
                    ref = uuid.uuid4().hex[:8].upper()
                job = {k:old[k] for k in ("invoice_id","customer_ref","account_id","payee",
                                          "amount_cents","currency","invoice_version","due_date")}
                job.update(id=ref, account_id=bank["id"], scheduled_for=when, status="awaiting_user",
                    user_approved=False, caregiver_approved=None, readback_delivered=False,
                    idempotency_key=uuid.uuid4().hex,
                    expires_on=min(bill["due_date"],(date.fromisoformat(self.today(state))+timedelta(days=1)).isoformat()))
                old["status"] = "superseded"
                state["jobs"][ref] = job
                self._emit(state, self._readback_text(state,job),job,"readback")
                self._audit(state,"simulation_replan_requires_fresh_approvals",job)
                break

    def flush(self):
        with self._tx() as (_, state):
            for item in state["outbox"]:
                if item["status"] == "failed" and item["kind"] != "caregiver_email":
                    item["status"] = "pending"
        super().flush()


class SimulationRuntime:
    def __init__(self, settings, twilio, directory, *, mailer, audit=None, today=None):
        self.settings, self.twilio = settings, twilio
        self.lock = threading.RLock()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        today = today or datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
        self.store = Store(self.directory / "account.sqlite3", today, settings.customer_first_name)
        self.bank = SimulatedBank(self.store)
        self.biller = SimulatedBiller(self.store)
        self.audit = audit or AuditLog(self.directory / "audit.sqlite3")
        self.payments = SimulationPayments(self.directory / "payments.sqlite3", self.biller, self.bank,
            twilio, settings.mom_whatsapp_to, runtime=self, mailer=mailer,
            customer_name=settings.customer_first_name, audit=self.audit)
        self.watcher = Watcher(Ledger(self.directory / "watch.sqlite3"), self.pull,
                               today=lambda: date.fromisoformat(self.store.state()["today"]))
        self.watcher.simulation_only = True
        self.last_error = None
        self.last_check = None
        self.stop_event = threading.Event()
        self.thread = None
        if self.store.state().get("reset_pending"):
            self._reset_derived()
        # Establish a baseline without sending historical transaction alerts.
        if not self.watcher.ledger.load_frame():
            self.watcher.run()

    def pull(self):
        snapshot = nessie_puller(self.bank, self.settings.customer_first_name)()
        state = self.payments.state()
        account = self.store.state()
        snapshot["source"] = "simulation"
        snapshot["balance_cents"] -= self.payments._spent_cents(state, account["account_id"])
        # Posted expenses already reduced settled cash. The watcher must not
        # subtract them a second time when judging their impact.
        snapshot["posted_purchases_in_balance"] = True
        return snapshot

    def tick(self):
        with self.lock:
            try:
                self.last_error = None
                self.payments.tick()
                self.watcher.run()
                self.payments.flush()
                self.flush_alerts()
                self.last_check = datetime.now(ZoneInfo("America/Chicago")).isoformat()
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.error("simulation check failed: %s", self.last_error)
                raise

    def flush_alerts(self):
        if not self.twilio.session_open(self.settings.mom_whatsapp_to):
            return
        for event in self.watcher.ledger.events("mom", False):
            if event.get("resolved") or not event.get("text"):
                continue
            try:
                sid = self.twilio.send_reply(self.settings.mom_whatsapp_to, event["text"])
            except Exception as exc:
                self.last_error = type(exc).__name__
                return
            self.watcher.ledger.mark_delivered(event["dedupe_key"])
            self.audit.append("reply", "Sent a simulation alert", text=event["text"], message_sid=sid)
        ledger = self.watcher.ledger
        with ledger._lock:
            pending = ledger._db.execute("SELECT seq,message FROM followups WHERE sent=0 AND fire_at<=?",
                (datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),)).fetchall()
        for row in pending:
            try:
                self.twilio.send_reply(self.settings.mom_whatsapp_to, row["message"])
            except Exception as exc:
                self.last_error = type(exc).__name__
                return
            with ledger._lock, ledger._db:
                ledger._db.execute("UPDATE followups SET sent=1 WHERE seq=?", (row["seq"],))

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()

        def run():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    pass  # tick records a sanitized error; the next pass retries
                self.stop_event.wait(10)

        self.thread = threading.Thread(target=run, daemon=True, name="simulation-monitor")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def state(self):
        with self.lock:
            account = self.store.state()
            payments = self.payments.state()
            paid = self.payments._spent_cents(payments, account["account_id"])
            alerts = self.watcher.ledger.events("mom")
            # Never expose caregiver tokens/hashes, bindings, or raw outbox.
            jobs = [{k:j.get(k) for k in ("id","invoice_id","payee","amount_cents","due_date","scheduled_for",
                     "status","user_approved","caregiver_approved","receipt")} for j in payments["jobs"].values()]
            messages = [{"id":m["id"], "text":m["body"], "status":m["status"], "kind":m["kind"]}
                        for m in payments["outbox"] if m["kind"] != "caregiver_email"]
            return {"simulation": True, "biller_mode":"local_simulation", "run_id":account["run_id"],
                    "today":account["today"], "name":account["name"],
                    "balance_cents":account["balance_cents"]-paid, "buffer_cents":self.payments.buffer,
                    "invoices":account["invoices"], "deposits":account["deposits"],
                    "purchases":account["purchases"][-30:], "receipts":list(account["receipts"].values()),
                    "jobs":jobs, "messages":messages[-50:], "alerts":alerts[-50:],
                    "events":account["events"][-50:], "audit":self.audit.events()[-60:],
                    "session_open":self.twilio.session_open(self.settings.mom_whatsapp_to),
                    "last_error":self.last_error, "last_check":self.last_check,
                    "presets":PRESETS, "saved":self.saved()}

    def _reset_derived(self):
        # Reset intent is durable. A crash here is completed on next startup.
        with self.payments._tx() as (db, state):
            state.clear()
            state.update(schema_version=1, demo_date=None, jobs={}, outbox=[], audit=[], wakeups=[])
            # Retain inbound SIDs: replayed old WhatsApp consent stays rejected.
        ledger = self.watcher.ledger
        with ledger._lock, ledger._db:
            for table in ("frame", "rhythm", "tape", "verdicts", "flags", "followups"):
                ledger._db.execute("DELETE FROM " + table)
        with self.store.tx() as account:
            account.pop("reset_pending", None)
        self.watcher.run()

    def reset(self, template="comfortable", saved_id=None):
        with self.lock:
            if any(j["status"] == "reconciling" for j in self.payments.state()["jobs"].values()):
                raise Conflict("A payment is still being confirmed. Wait before resetting.")
            if saved_id:
                with sqlite3.connect(self.store.database) as db:
                    row = db.execute("SELECT body FROM saved_scenarios WHERE id=?", (saved_id,)).fetchone()
                if not row:
                    raise ValueError("Saved scenario not found")
                fresh = json.loads(row[0])
                run = uuid.uuid4().hex
                fresh.update(run_id=run, account_id=run+"-account", receipts={}, applied={}, events=[])
                for collection, key in (("invoices","id"),("history","_id"),("deposits","_id"),("purchases","_id")):
                    for item in fresh[collection]:
                        item[key] = run + "-" + uuid.uuid4().hex[:12]
            else:
                if template not in {"comfortable", "waiting_income"}:
                    raise ValueError("Choose a known starting account")
                fresh = seed(self.store.state()["today"], self.settings.customer_first_name, template)
            with self.store.tx() as account:
                account.clear()
                account.update(fresh, reset_pending=True)
                self.store.event(account, "Started a new simulation run. Previous approvals no longer apply.")
            self._reset_derived()
            self.audit.append("decision", "Started a new simulation run", run_id=fresh["run_id"])

    def saved(self):
        with sqlite3.connect(self.store.database) as db:
            return [{"id":r[0], "name":r[1]} for r in db.execute("SELECT id,name FROM saved_scenarios ORDER BY name")]

    def save(self, name, request_id=None):
        name = label(name)
        account = self.store.state()
        account["balance_cents"] = self.state()["balance_cents"]
        if account["balance_cents"] < 0:
            raise ValueError("Cannot save an overdrawn starting account")
        account.update(receipts={}, events=[], applied={})
        with sqlite3.connect(self.store.database) as db:
            ident = request_id or uuid.uuid4().hex
            prior = db.execute("SELECT name FROM saved_scenarios WHERE id=?", (ident,)).fetchone()
            if prior and prior[0] != name:
                raise Conflict("Request id was already used for another saved scenario")
            db.execute("INSERT OR IGNORE INTO saved_scenarios VALUES(?,?,?)", (ident, name, json.dumps(account)))

    def command(self, operation, body, key):
        if not isinstance(body, dict):
            raise ValueError("Send a JSON object")
        if not isinstance(key, str) or not 8 <= len(key) <= 100:
            raise ValueError("A request id is required")
        fingerprint = hashlib.sha256(json.dumps([operation, body], sort_keys=True).encode()).hexdigest()
        with self.lock:
            account = self.store.state()
            if body.get("run_id") != account["run_id"]:
                raise Conflict("This run has changed. Refresh the dashboard and try again.")
            prior = account["applied"].get(key)
            if prior:
                if prior != fingerprint:
                    raise Conflict("Request id was already used for a different change")
                self.tick()
                return
            # The action and retry marker commit together for cash mutations.
            if operation == "expense":
                merchant, amount = label(body.get("merchant")), positive(body.get("amount"))
                when = iso(body.get("date", account["today"]))
                if when > account["today"]:
                    raise ValueError("A posted expense cannot be in the future")
                available = self.state()["balance_cents"]
                with self.store.tx() as state:
                    if available - amount < 0:
                        raise ValueError("The expense exceeds the simulated cash balance")
                    state["balance_cents"] -= amount
                    state["purchases"].append({"_id":uuid.uuid4().hex, "merchant_id":merchant,
                        "description":merchant, "amount":dollars(amount), "purchase_date":when, "status":"completed"})
                    self.store.event(state, f"Posted {money(amount)} at {merchant}.")
                    state["applied"][key] = fingerprint
            elif operation == "bill":
                payee, amount = label(body.get("payee")), positive(body.get("amount"))
                due = iso(body.get("due_date"))
                if not account["today"] <= due <= (date.fromisoformat(account["today"])+timedelta(days=90)).isoformat():
                    raise ValueError("Choose a bill due within the next 90 simulated days")
                # A single transaction updates invoice version and retry marker.
                with self.store.tx() as state:
                    row = next((b for b in state["invoices"] if b["payee"].casefold() == payee.casefold()
                                and b["status"] == "unpaid"), None)
                    if row:
                        row.update(amount_cents=amount, due_date=due, version=row["version"]+1)
                    else:
                        state["invoices"].append({"id":uuid.uuid4().hex, "customer_ref":"demo-margaret",
                            "payee":payee,"amount_cents":amount,"currency":"USD","version":1,
                            "due_date":due,"status":"unpaid"})
                    self.store.event(state, f"Set {payee} to {money(amount)}, due {due}.")
                    state["applied"][key] = fingerprint
            elif operation in {"delay-income", "settle-income"}:
                with self.store.tx() as state:
                    deposit = next((d for d in state["deposits"] if d["_id"] == body.get("deposit_id")
                                    and d["status"] == "pending"), None)
                    if not deposit:
                        raise ValueError("Choose a pending deposit")
                    if operation == "delay-income":
                        days = body.get("days", 7)
                        if type(days) is not int or not 1 <= days <= 31:
                            raise ValueError("Delay between 1 and 31 days")
                        deposit["transaction_date"] = (date.fromisoformat(deposit["transaction_date"])+timedelta(days=days)).isoformat()
                        self.store.event(state, f"Delayed {deposit['description']} by {days} days.")
                    else:
                        state["balance_cents"] += cents(deposit["amount"])
                        deposit.update(status="completed", transaction_date=state["today"])
                        self.store.event(state, f"Settled {money(cents(deposit['amount']))} income.")
                    state["applied"][key] = fingerprint
            elif operation == "advance":
                # One day per command: commit date, all due deposits, and retry
                # marker atomically, then run the existing payment checks.
                with self.store.tx() as state:
                    state["today"] = (date.fromisoformat(state["today"])+timedelta(days=1)).isoformat()
                    for deposit in state["deposits"]:
                        if deposit["status"] == "pending" and deposit["transaction_date"] <= state["today"]:
                            state["balance_cents"] += cents(deposit["amount"])
                            deposit["status"] = "completed"
                    self.store.event(state, "Advanced the simulation by one day; due income has settled.")
                    state["applied"][key] = fingerprint
            elif operation == "replan":
                with self.store.tx() as state:
                    self.store.event(state, "Requested fresh proposals for held bills. New approvals are required.")
                    state["applied"][key] = fingerprint
            elif operation == "check":
                with self.store.tx() as state:
                    state["applied"][key] = fingerprint
            else:
                raise ValueError("Unknown simulation action")
            self.tick()
