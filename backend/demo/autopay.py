"""Persistent bill automation demo. All money movement is SIMULATED locally.

SQLite serializes monitor, consent, deposits, cancellations, and settlement in
one transaction. Language models never authorize money or perform arithmetic.
No real Nessie or banking writes exist in this module.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .data import load_scenario

ACTIVE = {"awaiting_user", "awaiting_family", "scheduled"}


def cents(value) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError("Amount is missing")
    try:
        amount = Decimal(str(value)) * 100
    except InvalidOperation:
        raise ValueError("Invalid amount") from None
    if not amount.is_finite() or amount != amount.to_integral_value() or amount < 0:
        raise ValueError("Amount must be a nonnegative whole number of cents")
    return int(amount)


def dollars(value: int) -> str:
    return f"${Decimal(value) / 100:,.2f}"


def default_snapshot(as_of: date | None = None) -> dict:
    """A three-day bill, income on day 2, and a protected $200 balance."""
    snapshot = load_scenario("safe_payment", as_of=as_of)
    today = date.fromisoformat(snapshot["as_of"])
    upcoming = [b for b in snapshot["bills"] if b["status"] == "pending"]
    target = next(b for b in upcoming if b["payee"] == "Austin Energy")
    snapshot["bills"] = [b for b in snapshot["bills"] if b["status"] == "completed"] + [target]
    target["payment_date"] = (today + timedelta(days=3)).isoformat()
    snapshot["context"]["bill_metadata"] = {
        target["_id"]: {"due_date": target["payment_date"], "user_approved": False}}
    snapshot["account"]["balance"] = 300
    snapshot["deposits"] = [d for d in snapshot["deposits"] if d["status"] == "completed"]
    snapshot["deposits"].append({"_id": "synthetic-demo-next-income", "status": "pending",
                                  "amount": 1000, "description": "SYNTHETIC retirement income",
                                  "transaction_date": (today + timedelta(days=2)).isoformat()})
    snapshot["scenario_id"] = "three_day_autopay"
    snapshot["title"] = "Three days to go: approve now, pay after income"
    # The runner calculates its own decisions, never reads fixture expectations.
    snapshot.pop("expected", None)
    snapshot.pop("messages", None)
    return snapshot


class AutopayDemo:
    def __init__(self, database: str | Path, snapshot: dict | None = None, *, external=False):
        self.database = str(database)
        if self.database == ":memory:":
            raise ValueError("Use a file so each connection shares durable state")
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self.external = external
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
            # Kept across reset so an old Twilio retry can never approve a new run.
            db.execute("CREATE TABLE IF NOT EXISTS inbound (source TEXT, message_id TEXT, PRIMARY KEY(source,message_id))")
            db.execute("CREATE TABLE IF NOT EXISTS origin (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
            initial = snapshot or default_snapshot()
            db.execute("INSERT OR IGNORE INTO origin VALUES (1, ?)", (json.dumps(initial),))
            stored = db.execute("SELECT body FROM origin WHERE id=1").fetchone()[0]
            original = json.loads(stored)
            if snapshot is not None and (snapshot.get("scenario_id"), snapshot.get("as_of")) != (original.get("scenario_id"), original.get("as_of")):
                raise ValueError("This --db already belongs to a different scenario/date. Select a new --db.")
            db.execute("INSERT OR IGNORE INTO state VALUES (1, ?)", (json.dumps(self._initial(json.loads(stored))),))
            current = json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
            if current.get("external", False) != external:
                raise ValueError("Use a separate --db for local and WhatsApp modes")

    def _initial(self, snapshot: dict) -> dict:
        snapshot = deepcopy(snapshot)
        snapshot.pop("expected", None)
        snapshot.pop("messages", None)
        # A scenario's structured request is an explicit synthetic test input.
        request = snapshot["context"].get("payment_request")
        if request:
            bill = {"_id": "synthetic-requested-bill", "payee": request["payee"],
                    "payment_amount": float(Decimal(request["amount_cents"]) / 100),
                    "payment_date": request["date"], "status": "pending"}
            snapshot["bills"].append(bill)
            snapshot["context"]["bill_metadata"][bill["_id"]] = {
                "due_date": request["date"], "user_approved": False}
        return {"schema_version": 1, "synthetic": True, "external": self.external, "run_id": uuid.uuid4().hex,
                "today": snapshot["as_of"], "snapshot": snapshot,
                "balance_cents": cents(snapshot["account"]["balance"]),
                "requests": {}, "ledger": {}, "messages": [], "events": []}

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.database, timeout=15)
        try:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
            yield db, state
            state["snapshot"]["account"]["balance"] = float(Decimal(state["balance_cents"]) / 100)
            db.execute("UPDATE state SET body=? WHERE id=1", (json.dumps(state),))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def state(self) -> dict:
        with self._transaction() as (_, state):
            return deepcopy(state)

    def _event(self, state, kind, text, request_id=None):
        state["events"].append({"id": len(state["events"]) + 1, "date": state["today"],
                                "kind": kind, "text": text, "request_id": request_id})

    def _emit(self, state, actor, text, request_id=None):
        state["messages"].append({"id": uuid.uuid4().hex, "date": state["today"],
                                  "actor": actor, "direction": "out", "body": text,
                                  "request_id": request_id,
                                  "delivery": "pending" if self.external else "local"})
        req = state["requests"].get(request_id)
        if req and req["amount_cents"] is not None and req["payment_date"] and not self.external and text == self._prompt(req, family=actor == "family"):
            req["delivered_roles"] = sorted(set(req.get("delivered_roles", [])) | {actor})

    def _prompt(self, req, *, family=False):
        label = "[SIMULATED PAYMENT] "
        action = f"{dollars(req['amount_cents'])} to {req['payee']} on {req['payment_date']}"
        if family:
            return (label + f"Please review {action}. This is a new payee or a large payment. "
                    f"Reply APPROVE {req['id']} or DENY {req['id']}. No reply keeps it on hold.")
        return (label + f"Your {req['payee']} bill of {dollars(req['amount_cents'])} is due {req['due_date']}. "
                f"I suggest paying on {req['payment_date']}, keeping your protected cushion. "
                f"May I schedule {action}? Reply YES {req['id']} or NO {req['id']}.")

    def _safe(self, state, target, proposed: str) -> bool:
        """Reserve every other pending obligation through the latest known due date.

        This bounded demo assumes no unlisted future spending. Expected deposits
        support planning only; execution checks settled balance independently.
        """
        snap, today = state["snapshot"], date.fromisoformat(state["today"])
        bills = [b for b in snap["bills"] if b.get("status") == "pending"]
        dates = [b["payment_date"] for b in bills]
        end = max(dates + [proposed, state["today"]])
        if (date.fromisoformat(end) - today).days > 90:
            return False
        balance = state["balance_cents"]
        by_bill = {r["bill_id"]: r for r in state["requests"].values()}
        for offset in range(max(0, (date.fromisoformat(end) - today).days) + 1):
            when = (today + timedelta(days=offset)).isoformat()
            balance += sum(cents(d["amount"]) for d in snap["deposits"]
                           if d["status"] == "pending" and d["transaction_date"] == when)
            for bill in bills:
                req = by_bill.get(bill["_id"])
                when_due = (proposed if bill["_id"] == target["_id"] else
                            req["payment_date"] if req and req["status"] == "scheduled"
                            else bill["payment_date"])
                # Overdue obligations are still reserved immediately.
                if max(when_due, state["today"]) == when:
                    try:
                        balance -= cents(bill["payment_amount"])
                    except ValueError:
                        return False
            if balance < snap["context"]["buffer_cents"]:
                return False
        return True

    def _plan(self, state, bill):
        today, due = map(date.fromisoformat, (state["today"], bill["payment_date"]))
        if due < today or (due - today).days > 90:
            return None
        for offset in range((due - today).days + 1):
            proposed = (today + timedelta(days=offset)).isoformat()
            if self._safe(state, bill, proposed):
                return proposed
        return None

    def _approval_still_valid(self, state, req):
        bill = next((b for b in state["snapshot"]["bills"] if b["_id"] == req["bill_id"]), None)
        if bill is None:
            return False
        try:
            return (bill["payee"] == req["payee"] and cents(bill["payment_amount"]) == req["amount_cents"]
                    and bill["payment_date"] == req["due_date"] and bill["status"] == "pending"
                    and state["today"] <= req["payment_date"] <= req["due_date"]
                    and self._safe(state, bill, req["payment_date"]))
        except (ValueError, KeyError):
            return False

    def _tick(self, state):
        snap, now = state["snapshot"], state["today"]
        # Settle explicitly synthetic income before today's synthetic debits.
        for deposit in snap["deposits"]:
            key = state["run_id"] + ":deposit:" + deposit["_id"]
            if deposit["status"] == "pending" and deposit["transaction_date"] <= now:
                amount = cents(deposit["amount"])
                if key not in state["ledger"]:
                    state["balance_cents"] += amount
                    state["ledger"][key] = {"kind": "deposit", "amount_cents": amount, "date": now}
                    self._event(state, "deposit", f"Simulated income settled: {dollars(amount)}.")
                deposit["status"] = "completed"
        for req in state["requests"].values():
            if req["status"] in {"awaiting_user", "awaiting_family"} and (now > req["expires_on"] or now > req["due_date"]):
                req["status"] = "held"
                self._event(state, "held", "Approval expired. Silence never authorizes a payment.", req["id"])
                self._emit(state, "senior", f"The approval for {req['payee']} expired. No simulated payment was made.", req["id"])
            if req["status"] != "scheduled" or req["payment_date"] > now:
                continue
            bill = next((b for b in snap["bills"] if b["_id"] == req["bill_id"]), None)
            try:
                unchanged = (bill["payee"] == req["payee"] and cents(bill["payment_amount"]) == req["amount_cents"]
                             and bill["payment_date"] == req["due_date"] and bill["status"] == "pending")
                family_needed = (bill["payee"] not in snap["context"]["known_payees"]
                                 or req["amount_cents"] >= snap["context"]["family_threshold_cents"])
                safe = (req["user_approved"] and (not family_needed or req["family_approved"])
                        and unchanged and now == req["payment_date"] and now <= req["due_date"]
                        and state["balance_cents"] - req["amount_cents"] >= snap["context"]["buffer_cents"]
                        and self._safe(state, bill, now))
            except (ValueError, KeyError, TypeError):
                safe = False
            if not safe:
                req["status"] = "held"
                self._event(state, "held", "Final funds, date, bill identity, or approval check failed.", req["id"])
                self._emit(state, "senior", f"I put {req['payee']} on hold because its safety checks changed. No simulated payment was made.", req["id"])
                continue
            key = state["run_id"] + ":payment:" + bill["_id"]
            if key not in state["ledger"]:
                state["balance_cents"] -= req["amount_cents"]
                state["ledger"][key] = {"kind": "payment", "amount_cents": req["amount_cents"],
                                        "payee": req["payee"], "date": now, "request_id": req["id"],
                                        "balance_after_cents": state["balance_cents"]}
                self._event(state, "paid", f"Simulated payment recorded once: {dollars(req['amount_cents'])} to {req['payee']}.", req["id"])
                self._emit(state, "senior", f"[SIMULATED PAYMENT] Paid {dollars(req['amount_cents'])} to {req['payee']} on {now}. Your simulated balance is {dollars(state['balance_cents'])}.", req["id"])
            req["status"], bill["status"] = "paid", "completed"
        # One active read-back at a time, so a yes never selects among bills.
        if any(r["status"] in ACTIVE for r in state["requests"].values()):
            return
        seen = {r["bill_id"] for r in state["requests"].values()}
        candidates = sorted((b for b in snap["bills"] if b["status"] == "pending" and b["_id"] not in seen),
                            key=lambda b: (b["payment_date"], b["_id"]))
        for bill in candidates:
            delta = (date.fromisoformat(bill["payment_date"]) - date.fromisoformat(now)).days
            if delta > 3:
                continue
            try:
                amount = cents(bill["payment_amount"])
                proposed = self._plan(state, bill) if amount > 0 else None
            except ValueError:
                amount, proposed = None, None
            ref = uuid.uuid4().hex[:8].upper()
            while ref in state["requests"]:
                ref = uuid.uuid4().hex[:8].upper()
            req = {"id": ref, "bill_id": bill["_id"], "payee": bill["payee"],
                   "amount_cents": amount, "due_date": bill["payment_date"], "payment_date": proposed,
                   "status": "awaiting_user" if proposed else "blocked",
                   "requires_family": (amount is None or bill["payee"] not in snap["context"]["known_payees"]
                                       or amount >= snap["context"]["family_threshold_cents"]),
                   "user_approved": False, "family_approved": False,
                   "delivered_roles": [],
                   "expires_on": min(bill["payment_date"], (date.fromisoformat(now) + timedelta(days=1)).isoformat())}
            state["requests"][ref] = req
            if proposed:
                self._event(state, "proposal", f"Bill due in {delta} day(s). Earliest safe date: {proposed}.", ref)
                self._emit(state, "senior", self._prompt(req), ref)
                break
            self._event(state, "blocked", "No safe on-time date, invalid amount, or an unknown obligation.", ref)
            self._emit(state, "senior", f"I cannot safely schedule {bill['payee']} by {bill['payment_date']}. No simulated payment was made. Please review it with your trusted person.", ref)
            self._emit(state, "family", f"Please help review {bill['payee']}. Its simulated payment is blocked; no funds were sent.", ref)

    def tick(self):
        with self._transaction() as (_, state):
            self._tick(state)

    def advance(self, days: int = 1):
        if type(days) is not int or not 1 <= days <= 31:
            raise ValueError("Advance between 1 and 31 whole days")
        with self._transaction() as (_, state):
            self._tick(state)
            for _ in range(days):
                state["today"] = (date.fromisoformat(state["today"]) + timedelta(days=1)).isoformat()
                self._tick(state)

    def reset(self):
        with self._transaction() as (db, state):
            original = json.loads(db.execute("SELECT body FROM origin WHERE id=1").fetchone()[0])
            state.clear()
            state.update(self._initial(original))
            self._event(state, "reset", "Synthetic bank, approvals, messages, and clock reset. Old inbound ids remain deduplicated.")
            self._tick(state)

    def delay_income(self, days=7):
        if type(days) is not int or not 1 <= days <= 31:
            raise ValueError("Income delay must be 1 to 31 days")
        with self._transaction() as (_, state):
            deposits = [d for d in state["snapshot"]["deposits"] if d["status"] == "pending"]
            if not deposits:
                raise ValueError("There is no unsettled income to delay")
            deposit = min(deposits, key=lambda d: d["transaction_date"])
            deposit["transaction_date"] = (date.fromisoformat(deposit["transaction_date"]) + timedelta(days=days)).isoformat()
            self._event(state, "income_delayed", "Demo fault injected: expected income delayed. Settled balance is unchanged.")

    def receive(self, actor: str, text: str, message_id: str, *, request_id=None, source="local"):
        if not isinstance(actor, str) or actor not in {"senior", "family"} or not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("Use senior/family and a nonempty message up to 2000 characters")
        if not isinstance(message_id, str) or not message_id or len(message_id) > 128:
            raise ValueError("A unique message id is required")
        with self._transaction() as (db, state):
            if not db.execute("INSERT OR IGNORE INTO inbound VALUES (?,?)", (source, message_id)).rowcount:
                return False
            self._tick(state)
            state["messages"].append({"id": uuid.uuid4().hex, "date": state["today"], "actor": actor,
                                      "direction": "in", "body": text, "delivery": "received"})
            normalized = re.sub(r"[.!?,]+$", "", " ".join(text.lower().split()))
            match = re.fullmatch(r"(.+?)\s+([a-f0-9]{8})", normalized)
            command, ref = (match.group(1), match.group(2).upper()) if match else (normalized, request_id)
            req = state["requests"].get(ref)
            yes = command in ({"yes", "yes please", "yeah go ahead", "ok go ahead"} if actor == "senior" else {"approve", "yes"})
            no = command in {"no", "no thanks", "deny", "cancel", "undo", "cancel it", "undo that"}
            if yes or no:
                if not req:
                    self._emit(state, actor, "Please include the reference from the payment message, such as YES ABCD1234. I cannot apply a reply to an unspecified payment.")
                elif yes and self.external and actor not in req.get("delivered_roles", []):
                    self._emit(state, actor, "Please wait for the full payment read-back on this phone before approving. No payment was authorized.", ref)
                elif yes and req["status"] in {"awaiting_user", "awaiting_family"} and not self._approval_still_valid(state, req):
                    req["status"] = "held"
                    self._event(state, "held", "Bill or cash-flow facts changed after the read-back. A new proposal is required.", ref)
                    self._emit(state, "senior", "The bill or available funds changed. I put the simulated payment on hold; your old proposal was not used.", ref)
                elif no:
                    allowed = (req["status"] in {"awaiting_user", "awaiting_family"}
                               or (req["status"] == "scheduled" and state["today"] < req["payment_date"]))
                    if actor == "family":
                        allowed = allowed and req["requires_family"] and req["status"] == "awaiting_family"
                    if allowed:
                        req["status"] = "denied" if actor == "family" else "cancelled"
                        self._event(state, req["status"], "Payment permission withdrawn before execution.", ref)
                        self._emit(state, "senior", f"The simulated payment to {req['payee']} is on hold. No money will be sent for this request.", ref)
                        if actor == "family":
                            self._emit(state, "family", "Your denial was recorded. The simulated payment will not run.", ref)
                    else:
                        self._emit(state, actor, "That request cannot be cancelled here. It may already be complete or on hold; please check its status.", ref)
                elif actor == "senior" and req["status"] == "awaiting_user":
                    req["user_approved"] = True
                    req["status"] = "awaiting_family" if req["requires_family"] else "scheduled"
                    self._event(state, "user_approved", "Senior approved the exact payee, amount, and date in the read-back.", ref)
                    if req["requires_family"]:
                        self._emit(state, "family", self._prompt(req, family=True), ref)
                        self._emit(state, "senior", "Your approval is recorded. This simulated payment is held until your trusted person also approves.", ref)
                    else:
                        self._emit(state, "senior", f"[SIMULATED PAYMENT] Scheduled {dollars(req['amount_cents'])} to {req['payee']} on {req['payment_date']}. To cancel before that date, reply CANCEL {ref}.", ref)
                elif actor == "family" and req["status"] == "awaiting_family":
                    req["family_approved"] = True
                    req["status"] = "scheduled"
                    self._event(state, "family_approved", "Trusted person approved the same immutable request.", ref)
                    self._emit(state, "family", "Your approval is recorded for the simulated payment.", ref)
                    self._emit(state, "senior", f"[SIMULATED PAYMENT] Both approvals are recorded. Scheduled {dollars(req['amount_cents'])} to {req['payee']} on {req['payment_date']}. Reply CANCEL {ref} before that date to cancel.", ref)
                else:
                    self._emit(state, actor, "That approval does not apply now. The request may already be scheduled, complete, or expired.", ref)
            elif any(word in normalized for word in ("gift card", "crypto", "wire now", "shutoff")):
                self._emit(state, actor, "That message looks unusual. Do not send money or gift-card codes. Please check with your trusted person. I did not authorize any payment.")
            elif actor == "family":
                self._emit(state, actor, "I only share the flagged payment with you. Reply APPROVE or DENY with its reference; I do not share the account balance.")
            elif "balance" in normalized:
                self._emit(state, actor, f"Your simulated checking balance is {dollars(state['balance_cents'])}.")
            elif "bill" in normalized or "status" in normalized:
                rows = [f"{r['payee']}: {r['status']} (reference {r['id']})." for r in state["requests"].values()]
                self._emit(state, actor, " ".join(rows) or "No bills are inside the three-day monitoring window yet.")
            elif normalized in {"hi", "hello", "start", "ready"}:
                self._emit(state, actor, "Hello. This is Steady's simulated payment demo. Use the exact reference in the bill message to approve or cancel.")
            else:
                self._emit(state, actor, "I am not sure what you mean. Please reply YES, NO, or CANCEL with the reference from the bill message. Nothing was approved from this reply.")
            self._tick(state)
        return True

    def delivery(self, message_id: str, status: str, *, detail=None):
        if status not in {"sent", "awaiting_session", "failed", "unconfigured", "superseded"}:
            raise ValueError("Invalid delivery status")
        with self._transaction() as (_, state):
            for msg in state["messages"]:
                if msg["id"] == message_id:
                    msg["delivery"] = status
                    req = state["requests"].get(msg.get("request_id"))
                    if status == "sent" and req and req["amount_cents"] is not None and req["payment_date"] and msg["body"] == self._prompt(req, family=msg["actor"] == "family"):
                        req["delivered_roles"] = sorted(set(req.get("delivered_roles", [])) | {msg["actor"]})
                    if detail:
                        msg["delivery_detail"] = detail  # error type only, never exception text
                    break

    def resume_notifications(self, actor):
        with self._transaction() as (_, state):
            for msg in state["messages"]:
                if msg["actor"] == actor and msg["delivery"] in {"awaiting_session", "failed"}:
                    msg["delivery"] = "pending"

    def claim_wakeup(self, actor: str) -> bool:
        # Durable once per run/role. Templates are generic invitations, not approvals.
        with self._transaction() as (db, state):
            key = state["run_id"] + ":" + actor
            return bool(db.execute("INSERT OR IGNORE INTO inbound VALUES ('wakeup',?)", (key,)).rowcount)
