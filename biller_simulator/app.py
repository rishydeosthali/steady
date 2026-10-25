"""The external side of the demo: invoices and idempotent payment receipts.

Run this as a separate process (or on a separate machine). Steady can only
change its records through authenticated HTTP; it never opens this database.
"""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request


def create_biller(database: str | Path, token: str, *, as_of: date | None = None):
    if len(token) < 16:
        raise ValueError("BILLER_API_TOKEN must be at least 16 characters")
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    anchor = as_of or datetime.now(ZoneInfo("America/Chicago")).date()

    def connect():
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS invoices (
            id TEXT PRIMARY KEY, customer_ref TEXT NOT NULL, payee TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents>0), currency TEXT NOT NULL,
            due_date TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS payments (
            idempotency_key TEXT PRIMARY KEY, invoice_id TEXT UNIQUE NOT NULL,
            fingerprint TEXT NOT NULL, receipt TEXT NOT NULL)""")
        if db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0:
            for payee, amount, days in (("Austin Energy", 12000, 3), ("Water Co", 4525, 5), ("Spectrum", 8999, 7)):
                db.execute("INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?)", (
                    "demo-" + uuid.uuid4().hex, "demo-margaret", payee, amount, "USD",
                    (anchor + timedelta(days=days)).isoformat(), 1, "unpaid"))

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8192

    @app.before_request
    def authenticate():
        if request.path.startswith("/v1/"):
            supplied = request.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(supplied, ("Bearer " + token).encode()):
                return jsonify(error="Unauthorized"), 401

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
        return response

    @app.get("/")
    def dashboard():
        return render_template("biller.html")

    @app.get("/healthz")
    def health():
        return jsonify(service="demo-utility-company", simulation=True)

    @app.get("/v1/invoices")
    def invoices():
        customer = request.args.get("customer_ref", "")
        if not customer or len(customer) > 80:
            return jsonify(error="customer_ref is required"), 400
        with connect() as db:
            rows = db.execute("SELECT * FROM invoices WHERE customer_ref=? ORDER BY due_date,id", (customer,)).fetchall()
        return jsonify(simulation=True, invoices=[dict(row) for row in rows])

    @app.get("/v1/invoices/<invoice_id>")
    def invoice(invoice_id):
        with connect() as db:
            row = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return (jsonify(simulation=True, invoice=dict(row)) if row else (jsonify(error="Invoice not found"), 404))

    @app.post("/v1/invoices")
    def create_invoice():
        body = request.get_json(silent=True)
        fields = {"customer_ref", "payee", "amount_cents", "currency", "due_date"}
        if not isinstance(body, dict) or set(body) != fields:
            return jsonify(error="Use customer_ref, payee, amount_cents, currency, due_date"), 400
        if (not isinstance(body["customer_ref"], str) or not body["customer_ref"]
                or len(body["customer_ref"]) > 80
                or not isinstance(body["payee"], str) or not body["payee"].strip()
                or len(body["payee"]) > 80 or body["currency"] != "USD"
                or type(body["amount_cents"]) is not int or body["amount_cents"] <= 0):
            return jsonify(error="Invalid invoice"), 400
        try:
            due = date.fromisoformat(body["due_date"]).isoformat()
        except (TypeError, ValueError):
            return jsonify(error="Use an ISO due date"), 400
        payee = body["payee"].strip()
        customer = body["customer_ref"]
        db = connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM invoices WHERE customer_ref=? AND lower(trim(payee))=lower(?) "
                "AND status='unpaid' ORDER BY due_date,id",
                (customer, payee)).fetchone()
            if existing:
                db.execute(
                    "UPDATE invoices SET payee=?, amount_cents=?, due_date=?, version=version+1 WHERE id=?",
                    (payee, body["amount_cents"], due, existing["id"]))
                row = db.execute("SELECT * FROM invoices WHERE id=?", (existing["id"],)).fetchone()
            else:
                invoice_id = "inv-" + uuid.uuid4().hex
                db.execute("INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?)", (
                    invoice_id, customer, payee, body["amount_cents"], "USD", due, 1, "unpaid"))
                row = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            db.commit()
        finally:
            db.close()
        return jsonify(simulation=True, invoice=dict(row)), 201

    @app.get("/v1/payments")
    def all_payments():
        with connect() as db:
            rows = db.execute("SELECT receipt FROM payments ORDER BY rowid DESC").fetchall()
        return jsonify(simulation=True, payments=[json.loads(row[0]) for row in rows])

    @app.get("/v1/payments/<key>")
    def payment_lookup(key):
        with connect() as db:
            row = db.execute("SELECT receipt FROM payments WHERE idempotency_key=?", (key,)).fetchone()
        return jsonify(json.loads(row[0])) if row else (jsonify(error="Receipt not found"), 404)

    @app.post("/v1/payments")
    def pay():
        key = request.headers.get("Idempotency-Key", "")
        body = request.get_json(silent=True)
        fields = {"invoice_id", "customer_ref", "amount_cents", "currency", "invoice_version", "due_date", "simulation_date"}
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", key) or not isinstance(body, dict) or set(body) != fields:
            return jsonify(error="Use an Idempotency-Key and the documented payment fields"), 400
        if type(body["amount_cents"]) is not int or body["amount_cents"] <= 0 or type(body["invoice_version"]) is not int:
            return jsonify(error="Amounts and invoice versions must be positive integers"), 400
        try:
            simulated_date = date.fromisoformat(body["simulation_date"])
            due_date = date.fromisoformat(body["due_date"])
        except (ValueError, TypeError):
            return jsonify(error="Use ISO dates"), 400
        if any(not isinstance(body[name], str) for name in ("invoice_id", "customer_ref", "currency")):
            return jsonify(error="Invoice, customer and currency must be strings"), 400
        fingerprint = json.dumps(body, sort_keys=True, separators=(",", ":"))
        db = connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM payments WHERE idempotency_key=?", (key,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    return jsonify(error="Idempotency key reused with different payment details"), 409
                return jsonify(json.loads(prior["receipt"]))
            row = db.execute("SELECT * FROM invoices WHERE id=?", (body["invoice_id"],)).fetchone()
            if row is None:
                return jsonify(error="Invoice not found"), 404
            bill = dict(row)
            if any(body[a] != bill[b] for a,b in (("customer_ref","customer_ref"), ("amount_cents","amount_cents"),
                                                 ("currency","currency"), ("invoice_version","version"), ("due_date","due_date"))):
                return jsonify(error="The invoice changed; new approval is required"), 409
            if bill["status"] != "unpaid":
                return jsonify(error="This invoice already has a payment"), 409
            if simulated_date > due_date:
                return jsonify(error="The agreed invoice deadline has passed"), 409
            receipt = {"id": "sim-receipt-" + uuid.uuid4().hex, "simulation": True,
                       "status": "accepted", "invoice_id": bill["id"], "customer_ref": bill["customer_ref"],
                       "payee": bill["payee"], "amount_cents": bill["amount_cents"], "currency": bill["currency"],
                       "invoice_version": bill["version"], "due_date": bill["due_date"],
                       "simulation_date": body["simulation_date"], "idempotency_key": key,
                       "accepted_at": datetime.now(timezone.utc).isoformat()}
            db.execute("INSERT INTO payments VALUES (?,?,?,?)", (key, bill["id"], fingerprint, json.dumps(receipt)))
            db.execute("UPDATE invoices SET status='paid' WHERE id=?", (bill["id"],))
            db.commit()
            return jsonify(receipt), 201
        finally:
            db.close()  # also rolls back any rejected/incomplete transaction

    return app
