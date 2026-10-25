"""Approval and scheduler for an independent HTTP biller simulator.

Nessie supplies real sandbox reads. The biller simulates accepting a payment;
no Nessie debit or utility transfer is claimed. Accepted simulated spending is
reserved locally so an unchanged sandbox balance cannot be spent repeatedly.

A payment runs only after two separate approvals: the senior's reply to the
exact proposal, then the caregiver's decision on the emailed request. Silence
from either side is never consent.
"""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..nessie.client import NessieError
from .caregiver import MailError, binding, new_token, token_hash
from .client import BillerError
from .money import cents, money

YES_WORDS = r"yes|yes please|yeah|yep|ok|okay|sure|go ahead|yeah go ahead|yes go ahead|please do"
NO_WORDS = r"no|no thanks|cancel|undo|stop|don't|do not"
REPLY_RE = re.compile(rf"\s*({YES_WORDS}|{NO_WORDS})(?:\s+([a-fA-F0-9]{{8}}))?[.!]?\s*", re.I)
MAX_EMAIL_ATTEMPTS = 5
CANCELLABLE = {"awaiting_user", "awaiting_caregiver", "scheduled"}


def day(iso):
    d = date.fromisoformat(iso)
    return f"{d:%A}, {d:%b} {d.day}"


class PaymentService:
    def __init__(self, database, biller, nessie, twilio, recipient, *, mailer, customer_name="Margaret",
                 customer_ref="demo-margaret", demo_date=None, buffer_cents=20000,
                 known_payees=("Austin Energy", "Water Co", "Spectrum"), wakeup=None,
                 caregiver_name="your caregiver", max_cents=500000):
        if type(buffer_cents) is not int or buffer_cents < 0:
            raise ValueError("Protected buffer must be nonnegative integer cents")
        self.database, self.biller, self.nessie, self.twilio = str(database), biller, nessie, twilio
        self.recipient, self.customer_name, self.customer_ref = recipient, customer_name, customer_ref
        self.buffer, self.known_payees, self.wakeup = buffer_cents, set(known_payees), wakeup
        self.mailer, self.caregiver_name, self.max_cents = mailer, caregiver_name, max_cents
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
                job["scheduled_for"] if job and job["status"] in {"awaiting_caregiver","scheduled","reconciling"}
                else bill["due_date"])
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

    def _after_deposit(self, state, bank, when):
        """True when a pending deposit lands after today and on or before the planned date."""
        today = self.today(state)
        return when > today and any(
            d.get("status") == "pending" and today < str(d.get("transaction_date", ""))[:10] <= when
            for d in bank["deposits"])

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
            self._emit(state, f"Done. {job['payee']} confirmed your {money(job['amount_cents'])} payment. "
                       f"Receipt {receipt['id']}. (Demo biller; no real funds moved.)",job)

    def _approved(self, job):
        return (job["user_approved"] and job.get("caregiver_approved") is True
                and job.get("caregiver_binding") == binding(job["payee"],job["amount_cents"],job["scheduled_for"]))

    def _execute(self, state, job):
        # Always reconcile first, including after a crash, timeout, or lost reply.
        receipt = self.biller.receipt(job["idempotency_key"])
        if receipt is not None:
            self._paid(state,job,receipt)
            return
        invoices = self._invoices()
        invoice = next((b for b in invoices if b["id"] == job["invoice_id"]),None)
        today = self.today(state)
        if (not self._approved(job) or not job["readback_delivered"] or invoice is None
            or not self._matches(job,invoice) or invoice["status"] != "unpaid"
            or today != job["scheduled_for"] or today > job["due_date"]
            or job["payee"] not in self.known_payees or job["amount_cents"] > self.max_cents):
            job["status"] = "held"
            self._audit(state,"identity_date_or_approval_changed",job)
            self._emit(state,f"I did not pay {job['payee']}. The bill or the approvals changed, so I stopped. "
                       "Nothing was sent.",job)
            return
        bank = self._bank(state)
        if bank["id"] != job["account_id"] or bank["available"]-job["amount_cents"] < self.buffer or not self._safe(state,invoice,invoices,bank,today):
            job["status"] = "held"
            self._audit(state,"final_funds_check_failed",job)
            self._emit(state,f"I did not pay {job['payee']} today. Your account no longer has enough to pay "
                       f"it and keep your {money(self.buffer)} cushion. Nothing was sent.",job)
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
                    self._emit(state,"I didn't hear back, so I did not schedule that payment.",job)
                if job["status"] == "awaiting_caregiver":
                    if today > job["due_date"]:
                        job["status"] = "expired"
                        self._audit(state,"caregiver_never_answered",job)
                        self._emit(state,f"{self.caregiver_name.capitalize()} never approved the {job['payee']} payment, "
                                   "so I did not pay it.",job)
                    elif today >= job["scheduled_for"] and not job.get("waiting_notice_sent"):
                        job["waiting_notice_sent"] = True
                        self._emit(state,f"I'm still waiting for {self.caregiver_name} to approve the {job['payee']} "
                                   "payment. Nothing has been paid.",job)
                if job["status"] in {"scheduled","reconciling"} and job["scheduled_for"] <= today:
                    try:
                        self._execute(state,job)
                    except (BillerError,NessieError,ValueError) as exc:
                        # Retry with the SAME provider idempotency key. Do not
                        # label an uncertain external outcome cancelled or paid.
                        job["status"] = "reconciling"
                        job["last_error"] = type(exc).__name__
                        self._audit(state,"external_result_unconfirmed",job)
                        if not job.get("pending_notice_sent"):
                            job["pending_notice_sent"] = True
                            self._emit(state,f"Your {job['payee']} payment is still pending. I couldn't confirm it "
                                       "went through yet. I'll keep checking, and I won't pay it twice.",job)
            if any(j["status"] in {"awaiting_user","awaiting_caregiver","scheduled","reconciling"}
                   for j in state["jobs"].values()):
                return
            try:
                invoices, bank = self._invoices(), self._bank(state)
                seen = {(j["invoice_id"],j["invoice_version"]) for j in state["jobs"].values()}
                for bill in sorted(invoices,key=lambda b:(b["due_date"],b["id"])):
                    delta = (date.fromisoformat(bill["due_date"])-date.fromisoformat(today)).days
                    if bill["status"] != "unpaid" or (bill["id"],bill["version"]) in seen or delta > 3:
                        continue
                    when = self._plan(state,bill,invoices,bank)
                    known = bill["payee"] in self.known_payees and bill["amount_cents"] <= self.max_cents
                    allowed = known and when is not None
                    ref = uuid.uuid4().hex[:8].upper()
                    while ref in state["jobs"]:
                        ref = uuid.uuid4().hex[:8].upper()
                    job = {"id":ref,"invoice_id":bill["id"],"customer_ref":self.customer_ref,
                           "account_id":bank["id"],"payee":bill["payee"],"amount_cents":bill["amount_cents"],
                           "currency":"USD","invoice_version":bill["version"],"due_date":bill["due_date"],
                           "scheduled_for":when,"status":"awaiting_user" if allowed else "held",
                           "user_approved":False,"readback_delivered":False,"caregiver_approved":None,
                           "expires_on":min(bill["due_date"],(date.fromisoformat(today)+timedelta(days=1)).isoformat()),
                           "idempotency_key":uuid.uuid4().hex}
                    state["jobs"][ref] = job
                    amount = money(bill["amount_cents"])
                    if allowed:
                        when_text = "today" if when == today else day(when)
                        reason = " after your deposit arrives" if self._after_deposit(state,bank,when) else ""
                        self._emit(state,f"Your {amount} {bill['payee']} bill is due {day(bill['due_date'])}. "
                                   f"I recommend paying it {when_text}{reason}, so you keep at least "
                                   f"{money(self.buffer)} in your account. Would you like me to schedule it? "
                                   f"Reply YES or NO. (Ref {ref})",job,"readback")
                        self._audit(state,"proposal_from_external_invoice",job)
                        break
                    if not known:
                        text = (f"Your {amount} {bill['payee']} bill is due {day(bill['due_date'])}. It is a new "
                                "or unusually large payee, so I won't schedule it myself. Nothing has been paid.")
                    else:
                        text = (f"Your {amount} {bill['payee']} bill is due {day(bill['due_date'])}. I couldn't find "
                                f"a day to pay it on time and still keep your {money(self.buffer)} cushion, so I "
                                f"haven't scheduled anything. Please talk it over with {self.caregiver_name}.")
                    self._emit(state,text,job)
                    self._audit(state,"unsafe_or_family_review_required",job)
            except (BillerError,NessieError,ValueError) as exc:
                self._audit(state,"monitor_unavailable_" + type(exc).__name__)

    def _pick(self, state, ref, statuses):
        """One job for this reply: the named one, or the only one waiting. Never a guess."""
        if ref:
            job = state["jobs"].get(ref.upper())
            return (job, None) if job else (None, "I couldn't find that reference. Nothing was changed.")
        open_jobs = [j for j in state["jobs"].values() if j["status"] in statuses]
        if len(open_jobs) == 1:
            return open_jobs[0], None
        if not open_jobs:
            return None, "There's nothing waiting for your answer right now."
        refs = ", ".join(f"{j['payee']} (Ref {j['id']})" for j in open_jobs)
        return None, f"Which bill do you mean? {refs}. Reply with YES or NO and the reference."

    def handle(self, body, intent, sid):
        if not sid:
            return ["I need a signed phone reply with its message id before accepting a payment decision."]
        with self._tx() as (db,state):
            if not db.execute("INSERT OR IGNORE INTO inbound VALUES(?)",(sid,)).rowcount:
                return []
            match = REPLY_RE.fullmatch(body or "")
            saying_no = bool(match) and re.fullmatch(NO_WORDS, match.group(1), re.I) is not None
            if not match or (intent == "yes") == saying_no or intent not in {"yes","no","undo"}:
                self._emit(state,"Please answer with just YES or NO. I won't act on anything else.")
                return []
            job, problem = self._pick(state, match.group(2), CANCELLABLE if saying_no else {"awaiting_user"})
            if job is None:
                self._emit(state,problem)
                return []
            today = self.today(state)
            if saying_no:
                if job["status"] in CANCELLABLE:
                    job["status"] = "cancelled"
                    self._audit(state,"senior_cancelled",job)
                    self._emit(state,f"Okay, I won't pay {job['payee']}. Nothing was sent.",job)
                else:
                    self._emit(state,"I can't cancel that one. It was already sent or finished, "
                               "so I have to check the receipt first.",job)
            elif job["status"] == "awaiting_user" and job["readback_delivered"] and today <= job["expires_on"] \
                    and today <= job["scheduled_for"]:
                token = new_token()
                job["user_approved"], job["status"] = True, "awaiting_caregiver"
                job["caregiver_token_hash"] = token_hash(token)
                job["caregiver_binding"] = binding(job["payee"],job["amount_cents"],job["scheduled_for"])
                job["caregiver_email_attempts"] = 0
                self._audit(state,"senior_approved_exact_readback",job)
                state["outbox"].append({"id":uuid.uuid4().hex,"kind":"caregiver_email","token":token,
                                        "request_id":job["id"],"status":"pending","body":""})
                self._emit(state,f"Thank you. I've asked {self.caregiver_name} to approve paying "
                           f"{money(job['amount_cents'])} to {job['payee']} on {day(job['scheduled_for'])}. "
                           "Caregiver approval is pending, and nothing will be paid until they say yes. "
                           "Reply NO anytime before then to cancel.",job)
            else:
                self._emit(state,"That proposal isn't waiting for your answer anymore. Nothing was changed.",job)
        return []

    def _job_for_token(self, state, token):
        if not isinstance(token,str) or not 20 <= len(token) <= 100:
            return None
        wanted = token_hash(token)
        found = None
        for job in state["jobs"].values():
            if hmac.compare_digest(job.get("caregiver_token_hash") or "", wanted):
                found = job
        return found

    _STATUS_TEXT = {"scheduled":"You approved this payment.", "paid":"This payment is complete.",
                    "reconciling":"This payment was sent and is being confirmed.",
                    "denied":"You denied this payment. It will not be paid.",
                    "cancelled":"This payment was cancelled. Nothing will be paid.",
                    "expired":"This request expired. Nothing was paid.",
                    "held":"This payment is on hold. Nothing was paid."}

    def _caregiver_view(self, job):
        return {"payee":job["payee"],"amount":money(job["amount_cents"]),"date":day(job["scheduled_for"]),
                "status":job["status"],"senior":self.customer_name,
                "status_text":self._STATUS_TEXT.get(job["status"],"This request is no longer active.")}

    def caregiver_view(self, token):
        """Payee, amount, date, and status only. Never balance, deposits, or cushion."""
        with self._tx() as (_,state):
            job = self._job_for_token(state,token)
            return self._caregiver_view(job) if job else None

    def caregiver_decide(self, token, decision):
        if decision not in {"approve","deny"}:
            raise ValueError("Decision must be approve or deny")
        with self._tx() as (_,state):
            job = self._job_for_token(state,token)
            if job is None:
                return None, None
            if job["status"] != "awaiting_caregiver":
                return self._caregiver_view(job), None
            if job.get("caregiver_binding") != binding(job["payee"],job["amount_cents"],job["scheduled_for"]):
                job["status"] = "held"
                self._audit(state,"caregiver_binding_mismatch",job)
                return self._caregiver_view(job), "The payment details changed, so this approval no longer applies."
            today = self.today(state)
            if decision == "deny":
                job["status"], job["caregiver_approved"] = "denied", False
                self._audit(state,"caregiver_denied",job)
                self._emit(state,f"{self.caregiver_name.capitalize()} did not approve the {job['payee']} payment, "
                           f"so I have not paid it. The bill is still due {day(job['due_date'])}.",job)
                return self._caregiver_view(job), "Thank you. The payment will not be made."
            if today > job["scheduled_for"]:
                job["status"] = "held"
                self._audit(state,"caregiver_approved_after_planned_date",job)
                self._emit(state,f"{self.caregiver_name.capitalize()} approved the {job['payee']} payment after "
                           f"the planned day, so I did not pay it. Nothing was sent.",job)
                return self._caregiver_view(job), "This approval arrived after the planned date, so nothing was paid."
            job["status"], job["caregiver_approved"] = "scheduled", True
            self._audit(state,"caregiver_approved",job)
            when = "today" if job["scheduled_for"] == today else f"on {day(job['scheduled_for'])}"
            self._emit(state,f"{self.caregiver_name.capitalize()} approved it. I'll pay "
                       f"{money(job['amount_cents'])} to {job['payee']} {when} and let you know when it's "
                       "done. Reply NO before then to cancel.",job)
            return self._caregiver_view(job), "Thank you. The payment is approved."

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

    def _send_caregiver_email(self, state, item, job):
        if job is None or job["status"] != "awaiting_caregiver":
            item["status"], item["token"] = "superseded", None
            return
        try:
            self.mailer.request_approval({"payee":job["payee"],"amount":money(job["amount_cents"]),
                                          "date":day(job["scheduled_for"])}, item["token"])
        except MailError as exc:
            job["caregiver_email_attempts"] = job.get("caregiver_email_attempts", 0) + 1
            self._audit(state,"caregiver_email_failed_"+type(exc).__name__,job)
            if job["caregiver_email_attempts"] >= MAX_EMAIL_ATTEMPTS:
                item["status"], item["token"] = "failed", None
                self._emit(state,f"I couldn't reach {self.caregiver_name} by email, so the {job['payee']} "
                           "payment is on hold. Nothing has been paid.",job)
            return
        item["status"], item["token"] = "sent", None
        self._audit(state,"caregiver_email_sent",job)

    def flush(self):
        with self._tx() as (_,state):
            for item in state["outbox"]:
                if item["status"] not in {"pending","awaiting_session"}:
                    continue
                job = state["jobs"].get(item["request_id"])
                if item["kind"] == "caregiver_email":
                    self._send_caregiver_email(state,item,job)
                    continue
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
