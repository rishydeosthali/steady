"""Approval and scheduler for an independent HTTP biller simulator.

Nessie supplies real sandbox reads. The biller simulates accepting a payment;
no Nessie debit or utility transfer is claimed. Accepted simulated spending is
reserved locally so an unchanged sandbox balance cannot be spent repeatedly.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..nessie.client import NessieError
from .client import BillerError
from .money import cents, money


class PaymentService:
    def __init__(self, database, biller, nessie, twilio, recipient, *, customer_name="Margaret",
                 customer_ref="demo-margaret", demo_date=None, buffer_cents=20000,
                 known_payees=("Austin Energy", "Water Co", "Spectrum"), wakeup=None):
        if type(buffer_cents) is not int or buffer_cents < 0:
            raise ValueError("Protected buffer must be nonnegative integer cents")
        self.database, self.biller, self.nessie, self.twilio = str(database), biller, nessie, twilio
        self.recipient, self.customer_name, self.customer_ref = recipient, customer_name, customer_ref
        self.buffer, self.known_payees, self.wakeup = buffer_cents, set(known_payees), wakeup
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS inbound (sid TEXT PRIMARY KEY)")
            initial = {"schema_version":1, "demo_date":demo_date.isoformat() if demo_date else None,
                       "jobs":{}, "outbox":[], "audit":[], "wakeups":[]}
            db.execute("INSERT OR IGNORE INTO state VALUES(1,?)", (json.dumps(initial),))

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.database, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
            yield db, state
            db.execute("UPDATE state SET body=? WHERE id=1", (json.dumps(state),))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def today(self, state):
        return state["demo_date"] or datetime.now(ZoneInfo("America/Chicago")).date().isoformat()

    def state(self):
        with self._tx() as (_, state):
            result = json.loads(json.dumps(state))
        result["today"] = self.today(result)
        result["simulation"] = True
        result["bank_writes"] = False
        return result

    def _audit(self, state, event, job=None):
        state["audit"].append({"date":self.today(state), "event":event, "request_id":job["id"] if job else None})

    def _emit(self, state, body, job=None, kind="notification"):
        state["outbox"].append({"id":uuid.uuid4().hex, "body":body, "kind":kind,
                                 "request_id":job["id"] if job else None, "status":"pending"})

    def _invoices(self):
        invoices = self.biller.invoices(self.customer_ref)
        for bill in invoices:
            valid = (isinstance(bill, dict) and isinstance(bill.get("id"), str)
                     and bill.get("customer_ref") == self.customer_ref and bill.get("currency") == "USD"
                     and type(bill.get("amount_cents")) is int and bill["amount_cents"] > 0
                     and type(bill.get("version")) is int and bill["version"] > 0
                     and isinstance(bill.get("payee"), str) and bill["payee"]
                     and bill.get("status") in {"unpaid", "paid"})
            if not valid:
                raise BillerError("Invalid external invoice")
            try:
                date.fromisoformat(bill["due_date"])
            except (KeyError, TypeError, ValueError):
                raise BillerError("Invalid external invoice deadline") from None
        if len({b["id"] for b in invoices}) != len(invoices):
            raise BillerError("Duplicate external invoice ids")
        return invoices

    def _bank(self, state):
        account = self.nessie.resolve(self.customer_name)
        settled = cents(self.nessie.get_balance(account["_id"]))
        # Only accepted simulated payments consume the shadow budget. Nessie's
        # actual reported balance is never overwritten or represented as debited.
        spent = sum(j["amount_cents"] for j in state["jobs"].values()
                    if j["status"] == "paid" and j["account_id"] == account["_id"])
        return {"id":account["_id"], "available":settled-spent,
                "bills":self.nessie.get_bills(account["_id"]),
                "deposits":self.nessie.get_deposits(account["_id"])}

    def _safe(self, state, target, invoices, bank, proposed):
        today = date.fromisoformat(self.today(state))
        obligations = []
        jobs = {j["invoice_id"]:j for j in state["jobs"].values()}
        for bill in invoices:
            if bill["status"] != "unpaid":
                continue
            job = jobs.get(bill["id"])
            when = proposed if bill["id"] == target["id"] else (
                job["scheduled_for"] if job and job["status"] in {"scheduled","reconciling"} else bill["due_date"])
            obligations.append((when, bill["amount_cents"]))
        # Deduplicate only exact matching obligations; a different bill cycle
        # stays reserved even if its payee happens to have the same name.
        external = {(b["payee"],b["amount_cents"],b["due_date"]) for b in invoices}
        for bill in bank["bills"]:
            if str(bill.get("status", "")).lower() in {"completed","cancelled","canceled"}:
                continue
            amount = cents(bill.get("payment_amount"))
            when = str(bill.get("payment_date") or bill.get("upcoming_payment_date") or "")[:10]
            date.fromisoformat(when)
            if (bill.get("payee"), amount, when) not in external:
                obligations.append((when,amount))
        end = max([when for when,_ in obligations] + [proposed,self.today(state)])
        horizon = (date.fromisoformat(end)-today).days
        if horizon > 90:
            return False
        balance = bank["available"]
        for offset in range(max(0,horizon)+1):
            when = (today+timedelta(days=offset)).isoformat()
            for deposit in bank["deposits"]:
                if deposit.get("status") == "pending" and when > self.today(state) and str(deposit.get("transaction_date", ""))[:10] == when:
                    balance += cents(deposit.get("amount"))
            balance -= sum(amount for deadline,amount in obligations if max(deadline,self.today(state)) == when)
            if balance < self.buffer:
                return False
        return True

    def _plan(self, state, bill, invoices, bank):
        today, due = map(date.fromisoformat,(self.today(state),bill["due_date"]))
        if due < today or (due-today).days > 90:
            return None
        for offset in range((due-today).days+1):
            when = (today+timedelta(days=offset)).isoformat()
            if self._safe(state,bill,invoices,bank,when):
                return when
        return None

    def _matches(self, job, invoice):
        return all(job[a] == invoice.get(b) for a,b in (
            ("invoice_id","id"),("customer_ref","customer_ref"),("payee","payee"),
            ("amount_cents","amount_cents"),("currency","currency"),("invoice_version","version"),("due_date","due_date")))

    def _validate_receipt(self, job, receipt):
        if not isinstance(receipt,dict) or receipt.get("simulation") is not True or receipt.get("status") != "accepted":
            raise BillerError("Invalid receipt")
        if any(receipt.get(name) != job[name] for name in ("invoice_id","customer_ref","payee","amount_cents","currency","invoice_version","due_date","idempotency_key")):
            raise BillerError("Receipt does not match the approved payment")
        if receipt.get("simulation_date") != job["scheduled_for"] or not isinstance(receipt.get("id"),str) or not receipt["id"]:
            raise BillerError("Receipt date or id does not match")

    def _paid(self, state, job, receipt):
        self._validate_receipt(job, receipt)
        if job["status"] != "paid":
            job["status"], job["receipt"] = "paid", receipt
            self._audit(state,"external_receipt_confirmed",job)
            self._emit(state, f"[SIMULATED BILLER] {job['payee']} accepted {money(job['amount_cents'])}. "
                       f"Receipt {receipt['id']}. This is a demo payment; no real funds moved.",job)

    def _execute(self, state, job):
        # Always reconcile first, including after a crash, timeout, or lost reply.
        receipt = self.biller.receipt(job["idempotency_key"])
        if receipt is not None:
            self._paid(state,job,receipt)
            return
        invoices = self._invoices()
        invoice = next((b for b in invoices if b["id"] == job["invoice_id"]),None)
        today = self.today(state)
        if (not job["user_approved"] or not job["readback_delivered"] or invoice is None
            or not self._matches(job,invoice) or invoice["status"] != "unpaid"
            or today != job["scheduled_for"] or today > job["due_date"]
            or job["payee"] not in self.known_payees or job["amount_cents"] >= 50000):
            job["status"] = "held"
            self._audit(state,"identity_date_or_approval_changed",job)
            self._emit(state,"The demo payment is on hold because its details or approvals changed. No new payment was sent.",job)
            return
        bank = self._bank(state)
        if bank["id"] != job["account_id"] or bank["available"]-job["amount_cents"] < self.buffer or not self._safe(state,invoice,invoices,bank,today):
            job["status"] = "held"
            self._audit(state,"final_funds_check_failed",job)
            self._emit(state,f"I held the demo payment to {job['payee']}; the latest funds check did not preserve your cushion.",job)
            return
        payload = {"invoice_id":job["invoice_id"],"customer_ref":job["customer_ref"],
                   "amount_cents":job["amount_cents"],"currency":job["currency"],
                   "invoice_version":job["invoice_version"],"due_date":job["due_date"],"simulation_date":job["scheduled_for"]}
        job["status"] = "reconciling"
        receipt = self.biller.pay(job["idempotency_key"], payload)
        self._paid(state,job,receipt)

    def tick(self):
        with self._tx() as (_, state):
            today = self.today(state)
            for job in state["jobs"].values():
                if job["status"] == "awaiting_user" and today > job["expires_on"]:
                    job["status"] = "expired"
                    self._emit(state,"Your demo payment proposal expired. No approval was assumed.",job)
                if job["status"] in {"scheduled","reconciling"} and job["scheduled_for"] <= today:
                    try:
                        self._execute(state,job)
                    except (BillerError,NessieError,ValueError) as exc:
                        # Retry with the SAME provider idempotency key. Do not
                        # label an uncertain external outcome cancelled or paid.
                        job["status"] = "reconciling"
                        job["last_error"] = type(exc).__name__
                        self._audit(state,"external_result_unconfirmed",job)
            if any(j["status"] in {"awaiting_user","scheduled","reconciling"} for j in state["jobs"].values()):
                return
            try:
                invoices, bank = self._invoices(), self._bank(state)
                seen = {(j["invoice_id"],j["invoice_version"]) for j in state["jobs"].values()}
                for bill in sorted(invoices,key=lambda b:(b["due_date"],b["id"])):
                    delta = (date.fromisoformat(bill["due_date"])-date.fromisoformat(today)).days
                    if bill["status"] != "unpaid" or (bill["id"],bill["version"]) in seen or delta > 3:
                        continue
                    when = self._plan(state,bill,invoices,bank)
                    allowed = bill["payee"] in self.known_payees and bill["amount_cents"] < 50000 and when is not None
                    ref = uuid.uuid4().hex[:8].upper()
                    while ref in state["jobs"]:
                        ref = uuid.uuid4().hex[:8].upper()
                    job = {"id":ref,"invoice_id":bill["id"],"customer_ref":self.customer_ref,
                           "account_id":bank["id"],"payee":bill["payee"],"amount_cents":bill["amount_cents"],
                           "currency":"USD","invoice_version":bill["version"],"due_date":bill["due_date"],
                           "scheduled_for":when,"status":"awaiting_user" if allowed else "held",
                           "user_approved":False,"readback_delivered":False,
                           "expires_on":min(bill["due_date"],(date.fromisoformat(today)+timedelta(days=1)).isoformat()),
                           "idempotency_key":uuid.uuid4().hex}
                    state["jobs"][ref] = job
                    if allowed:
                        self._emit(state,f"[SIMULATED BILLER] Your {bill['payee']} bill of {money(bill['amount_cents'])} "
                                   f"is due in {delta} day(s), on {bill['due_date']}. May I pay {money(bill['amount_cents'])} "
                                   f"to {bill['payee']} on {when}, keeping your {money(self.buffer)} cushion? "
                                   f"Reply YES {ref} or NO {ref}.",job,"readback")
                        self._audit(state,"proposal_from_external_invoice",job)
                        break
                    self._emit(state,f"I held {bill['payee']}'s demo bill. A new/large payee needs family review, "
                               "or there is no safe date. This integration does not bypass that review.",job)
                    self._audit(state,"unsafe_or_family_review_required",job)
            except (BillerError,NessieError,ValueError) as exc:
                self._audit(state,"monitor_unavailable_" + type(exc).__name__)

    def handle(self, body, intent, sid):
        if not sid:
            return ["I need a signed phone reply with its message id before accepting a demo payment decision."]
        with self._tx() as (db,state):
            if not db.execute("INSERT OR IGNORE INTO inbound VALUES(?)",(sid,)).rowcount:
                return []
            match = re.fullmatch(r"\s*(yes|yes please|yeah go ahead|no|cancel|undo)\s+([a-fA-F0-9]{8})[.!]?\s*",body,re.I)
            job = state["jobs"].get(match.group(2).upper()) if match else None
            if job is None:
                self._emit(state,"Please use the exact reference in the bill message, such as YES ABCD1234. "
                           "I will not choose a bill or amount from a vague reply.")
                return []
            command = match.group(1).lower()
            today = self.today(state)
            if command in {"no","cancel","undo"}:
                if job["status"] == "awaiting_user" or (job["status"] == "scheduled" and today < job["scheduled_for"]):
                    job["status"] = "cancelled"
                    self._emit(state,"That demo payment is cancelled before submission to the utility.",job)
                else:
                    self._emit(state,"I cannot cancel an already submitted or completed payment. Its external receipt must be checked first.",job)
            elif (job["status"] == "awaiting_user" and job["readback_delivered"] and today <= job["expires_on"]
                  and today <= job["scheduled_for"] and intent == "yes"):
                job["user_approved"],job["status"] = True,"scheduled"
                self._audit(state,"senior_approved_exact_readback",job)
                self._emit(state,f"[SIMULATED BILLER] Scheduled {money(job['amount_cents'])} to {job['payee']} "
                           f"on {job['scheduled_for']}. Reply CANCEL {job['id']} before that date to cancel.",job)
            else:
                self._emit(state,"That proposal is not available for approval. Wait for the full read-back or ask for its status.",job)
        return []

    def bill_summary(self):
        try:
            invoices = self._invoices()
        except BillerError:
            return ["I cannot read the demo utility's invoices right now. I will not guess their payment status."]
        state = self.state()
        jobs = {j["invoice_id"]:j for j in state["jobs"].values()}
        rows = []
        for invoice in sorted(invoices,key=lambda b:(b["due_date"],b["id"])):
            job = jobs.get(invoice["id"])
            receipt = job.get("receipt") if job else None
            rows.append(f"{invoice['payee']}: {money(invoice['amount_cents'])}, due {invoice['due_date']}, "
                        f"{invoice['status']} at the demo utility" + (f", receipt {receipt['id']}" if receipt else "") + ".")
        return ["[SIMULATED BILLER] " + (" ".join(rows) or "No invoices found.") +
                " These are the utility simulator's records; Nessie's bank balance is unchanged by these demo payments."]

    def flush(self):
        with self._tx() as (_,state):
            for item in state["outbox"]:
                if item["status"] not in {"pending","awaiting_session"}:
                    continue
                job = state["jobs"].get(item["request_id"])
                if item["kind"] == "readback" and job and (job["status"] != "awaiting_user" or self.today(state)>job["expires_on"]):
                    item["status"] = "superseded"
                    continue
                if not self.twilio.session_open(self.recipient):
                    item["status"] = "awaiting_session"
                    wake_key = self.recipient + ":" + self.today(state)
                    if self.wakeup and wake_key not in state["wakeups"]:
                        state["wakeups"].append(wake_key)
                        try:
                            self.wakeup()
                        except Exception as exc:
                            self._audit(state,"wakeup_failed_" + type(exc).__name__)
                    continue
                try:
                    self.twilio.send_reply(self.recipient,item["body"])
                except Exception as exc:
                    item["status"] = "failed"
                    self._audit(state,"notification_failed_"+type(exc).__name__,job)
                    continue
                item["status"] = "sent"
                if item["kind"] == "readback" and job:
                    job["readback_delivered"] = True

    def advance(self, days):
        if type(days) is not int or not 1 <= days <= 31:
            raise ValueError("Advance between 1 and 31 simulated days")
        for _ in range(days):
            with self._tx() as (_,state):
                if state["demo_date"] is None:
                    raise ValueError("Set STEADY_BILLER_DEMO_DATE to enable clock simulation")
                state["demo_date"] = (date.fromisoformat(state["demo_date"])+timedelta(days=1)).isoformat()
            self.tick()
