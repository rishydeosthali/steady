"""SQLite memory: frame, rhythm, tape, verdicts.

Delete the file and the next pass is a first look. No transcripts, no
embeddings. The tape is append-only and ``record_decision`` is its only writer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .judge import DECISIONS
from .units import key_of

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "ledger.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frame (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rhythm (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS tape (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    payee_key TEXT NOT NULL,
    payee TEXT,
    amount_cents INTEGER,
    payment_date TEXT,
    decision TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verdicts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    consumer TEXT NOT NULL,
    notify INTEGER NOT NULL,
    source TEXT NOT NULL,
    sheet TEXT NOT NULL,
    text TEXT,
    delivered INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flags (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS followups (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    fire_at TEXT NOT NULL,
    message TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0
);
"""


class DecisionError(ValueError):
    """A tape row was rejected."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str | Path = DEFAULT_PATH):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path) if str(path) != ":memory:" else None
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- frame ------------------------------------------------------------

    def load_frame(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT id, fingerprint FROM frame").fetchall()
        return {r["id"]: json.loads(r["fingerprint"]) for r in rows}

    def save_frame(self, frame: dict[str, dict[str, Any]]) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM frame")
            self._db.executemany("INSERT INTO frame (id, fingerprint) VALUES (?, ?)",
                                 [(i, json.dumps(fp, sort_keys=True)) for i, fp in frame.items()])

    # -- rhythm -----------------------------------------------------------

    def save_rhythm(self, rhythm: dict[str, dict[str, dict[str, Any]]]) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM rhythm")
            self._db.executemany(
                "INSERT INTO rhythm (kind, key, data) VALUES (?, ?, ?)",
                [(kind, key, json.dumps(data, sort_keys=True))
                 for kind, group in rhythm.items() for key, data in group.items()])

    def load_rhythm(self) -> dict[str, dict[str, dict[str, Any]]]:
        with self._lock:
            rows = self._db.execute("SELECT kind, key, data FROM rhythm").fetchall()
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["kind"], {})[r["key"]] = json.loads(r["data"])
        return out

    # -- tape -------------------------------------------------------------

    def record_decision(self, payee: str, decision: str, *, amount_cents: int | None = None,
                        payment_date: date | str | None = None,
                        recorded_at: str | None = None) -> dict[str, Any]:
        """Append one decision Mom or Sarah actually made. The tape's only writer."""
        payee_key = key_of(payee)
        if not payee_key:
            raise DecisionError("payee is required")
        if decision not in DECISIONS:
            raise DecisionError(f"decision must be one of {', '.join(DECISIONS)}")
        if amount_cents is not None and (isinstance(amount_cents, bool)
                                         or not isinstance(amount_cents, int)):
            raise DecisionError("amount_cents must be an integer")
        if payment_date is not None:
            try:
                payment_date = date.fromisoformat(str(payment_date)[:10]).isoformat()
            except ValueError:
                raise DecisionError("payment_date must be YYYY-MM-DD") from None
        row = {"payee_key": payee_key, "payee": payee, "amount_cents": amount_cents,
               "payment_date": payment_date, "decision": decision,
               "recorded_at": recorded_at or _now()}
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO tape (payee_key, payee, amount_cents, payment_date, decision, recorded_at)"
                " VALUES (:payee_key, :payee, :amount_cents, :payment_date, :decision, :recorded_at)",
                row)
        return {"seq": cur.lastrowid, **row}

    def tape(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM tape ORDER BY seq").fetchall()
        return [dict(r) for r in rows]

    # -- verdicts ---------------------------------------------------------

    def seen(self, dedupe_key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM verdicts WHERE dedupe_key = ?",
                                    (dedupe_key,)).fetchone() is not None

    def store_verdict(self, sheet: dict[str, Any], text: str | None = None,
                      delivered: bool = False) -> bool:
        """Store a new verdict. False if its dedupe key was already stored."""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO verdicts (dedupe_key, kind, consumer, notify, source, sheet,"
                " text, delivered, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sheet["dedupe_key"], sheet["kind"], sheet["consumer"], int(sheet["notify"]),
                 sheet["source"], json.dumps(sheet, sort_keys=True), text, int(delivered), _now()))
        return cur.rowcount == 1

    def mark_delivered(self, dedupe_key: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE verdicts SET delivered = 1 WHERE dedupe_key = ?", (dedupe_key,))

    def resolve_missing(self, open_keys: set[str]) -> list[str]:
        """Clear deposit_missing flags whose deposit has now appeared. Sends nothing."""
        with self._lock, self._db:
            rows = self._db.execute("SELECT dedupe_key FROM verdicts WHERE kind = 'deposit_missing'"
                                    " AND resolved = 0").fetchall()
            cleared = [r["dedupe_key"] for r in rows if r["dedupe_key"] not in open_keys]
            self._db.executemany("UPDATE verdicts SET resolved = 1 WHERE dedupe_key = ?",
                                 [(k,) for k in cleared])
        return cleared

    def set_hold_scheduling(self, hold: bool) -> None:
        """Keep the planner queue's hold flag in step with an open missing deposit."""
        with self._lock, self._db:
            rows = self._db.execute("SELECT seq, sheet FROM verdicts WHERE kind = 'bill_due'"
                                    " AND delivered = 0").fetchall()
            for r in rows:
                sheet = json.loads(r["sheet"])
                if sheet["facts"].get("hold_scheduling") != hold:
                    sheet["facts"]["hold_scheduling"] = hold
                    self._db.execute("UPDATE verdicts SET sheet = ? WHERE seq = ?",
                                     (json.dumps(sheet, sort_keys=True), r["seq"]))

    def events(self, consumer: str | None = None, delivered: bool | None = None) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM verdicts WHERE 1 = 1", []
        if consumer:
            query += " AND consumer = ?"
            args.append(consumer)
        if delivered is not None:
            query += " AND delivered = ?"
            args.append(int(delivered))
        with self._lock:
            rows = self._db.execute(query + " ORDER BY seq", args).fetchall()
        return [{**json.loads(r["sheet"]), "text": r["text"], "delivered": bool(r["delivered"]),
                 "resolved": bool(r["resolved"]), "created_at": r["created_at"]} for r in rows]

    def open_alerts(self) -> list[dict[str, Any]]:
        """Mom-facing alerts that still need an answer."""
        return [e for e in self.events(consumer="mom") if e.get("notify") and not e.get("resolved")]

    def resolve_alert(self, dedupe_key: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("UPDATE verdicts SET resolved = 1 WHERE dedupe_key = ? AND resolved = 0",
                                   (dedupe_key,))
        return cur.rowcount == 1

    def flag_transaction(self, dedupe_key: str, reason: str) -> bool:
        """Mark a known alert as unexpected. No-op when the id is unknown."""
        reason = (reason or "unexpected").strip()[:200]
        with self._lock, self._db:
            row = self._db.execute("SELECT 1 FROM verdicts WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
            if row is None:
                return False
            self._db.execute("INSERT INTO flags (dedupe_key, reason, created_at) VALUES (?, ?, ?)",
                             (dedupe_key, reason, _now()))
            self._db.execute("UPDATE verdicts SET resolved = 1 WHERE dedupe_key = ?", (dedupe_key,))
        return True

    def flags(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM flags ORDER BY seq").fetchall()
        return [dict(r) for r in rows]

    def schedule_followup(self, minutes: int, message: str) -> dict[str, Any] | None:
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 24 * 60:
            return None
        text = (message or "").strip()
        if not text:
            return None
        fire_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        with self._lock, self._db:
            cur = self._db.execute("INSERT INTO followups (fire_at, message, sent) VALUES (?, ?, 0)",
                                   (fire_at, text[:400]))
        return {"id": cur.lastrowid, "fire_at": fire_at, "message": text[:400]}

    def due_followups(self, now: datetime | None = None) -> list[str]:
        when = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        with self._lock, self._db:
            rows = self._db.execute("SELECT seq, message FROM followups WHERE sent = 0 AND fire_at <= ?",
                                    (when,)).fetchall()
            self._db.executemany("UPDATE followups SET sent = 1 WHERE seq = ?", [(r["seq"],) for r in rows])
        return [r["message"] for r in rows]
