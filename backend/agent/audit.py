"""Append-only decision log. The model never writes this file.

Every intent, tool, payment decision, and reply is one new row. Rows are
never updated or deleted. After a quiet pause, Steady may offer a recap
built only from those rows.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "audit.sqlite"
PAUSE_SECONDS = 60
OFFER = "Would you like a short summary of what we just did?"
DECLINE = "Okay."
NOTHING = "There is nothing new to recap yet."
_SUMMARY_ASK = re.compile(
    r"\b(summary|summarize|summarise|recap)\b|"
    r"what (just )?happened|what did (we|you) do|catch me up|"
    r"\blog\s+history\b|\b(?:the|our|my|chat|decision|activity)\s+log\b|"
    r"\b(?:the|our|my)\s+history\b",
    re.IGNORECASE,
)

KINDS = (
    "intent", "tool", "decision", "reply", "receipt",
    "question", "contemplate", "blocked", "offer", "summary",
)

_RECAP_SKIP = {"intent", "reply", "offer", "summary"}
_RECAP_HIDE = {
    "bill photo reply handled in code",
    "payment reply handled in code",
    "used the backup reply",
    "guide finished",
    "guide unavailable, used backup",
    "read a bill photo",
    "intent parse failed",
    "photo intake is not attached",
    "asked again about the bill photo",
    "unclear text, asked again",
    "that action is not available",
    "needed an amount",
    "needed a payee",
    "account was not visible",
    "payment waiting",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime | None = None) -> str:
    return (when or _now()).isoformat(timespec="seconds")


def asks_for_summary(text: str) -> bool:
    """True when she asked for a recap. Code decides; the model does not."""
    return bool(_SUMMARY_ASK.search((text or "").strip()))


def _sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return text if text.endswith((".", "?", "!")) else text + "."


def _who(detail: dict[str, Any]) -> str:
    return str(detail.get("payee") or detail.get("merchant") or "").strip()


def _amount(detail: dict[str, Any]) -> str:
    return str(detail.get("amount") or "").strip()


def _named(detail: dict[str, Any], noun: str) -> str:
    who, amount = _who(detail), _amount(detail)
    if who and amount:
        return f"{amount} to {who}"
    if who:
        return f"the {who} {noun}"
    if amount:
        return f"{amount}"
    return f"a {noun}"


def _join_names(names: list[str]) -> str:
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + ", and " + names[-1]


def _beat(event: dict[str, Any]) -> tuple[str, str, str] | None:
    """(group, dedupe_key, sentence) from one stored row. None to omit."""
    kind = event.get("kind") or ""
    reason = (event.get("reason") or "").strip()
    detail = event.get("detail") or {}
    name = str(detail.get("event") or "")
    who = _who(detail)
    if kind in _RECAP_SKIP or reason.casefold() in _RECAP_HIDE:
        return None
    if kind == "tool" and detail.get("ran") is False:
        return None
    if name.startswith(("monitor_unavailable", "wakeup_failed", "notification_failed",
                        "caregiver_email_failed")):
        return None

    if kind == "question" or reason.startswith("You asked"):
        needs = detail.get("needs")
        if isinstance(needs, list) and needs:
            asked = " and ".join(str(n) for n in needs)
            return ("asked", asked, _sentence(f"You asked about {asked}"))
        return ("asked", reason, _sentence(reason or "You asked about your account"))

    if "Paying is not turned on" in reason:
        return ("blocked", "pay", "You asked me to pay something. Paying is not turned on, so I did not send money.")
    if reason == "not Margaret":
        return ("blocked", "outsider",
                "Someone asked for something I only do for you. "
                "I did not share account details or skip a caregiver check.")

    if kind == "contemplate" and "Checked whether" in reason:
        return ("check", who or reason, _sentence(reason))

    if name == "proposal_from_external_invoice" or reason == "proposal from external invoice":
        label = _named(detail, "bill")
        return ("proposal", who or label, f"I sent you a payment proposal for {label}.")

    if name == "senior_approved_exact_readback":
        return ("approved", who, _sentence(f"You approved {_named(detail, 'payment')}"))
    if name in {"senior_cancelled", "senior_held"}:
        return ("stopped", who, _sentence(f"You asked me to stop {_named(detail, 'payment')}"))
    if name == "caregiver_approved":
        return ("caregiver", who, _sentence(f"Your caregiver approved {_named(detail, 'payment')}"))
    if name == "caregiver_denied":
        return ("caregiver", who, _sentence(f"Your caregiver did not approve {_named(detail, 'payment')}"))
    if name == "unsafe_or_family_review_required":
        return ("hold", who, _sentence(f"{_named(detail, 'payment').capitalize()} is waiting for your caregiver"))
    if name == "caregiver_email_sent":
        return ("email", who, "I emailed your caregiver about that payment.")

    if kind == "receipt" or "receipt" in name:
        if "photo" in reason.casefold() or "Nessie" in reason:
            return ("photo", "saved",
                    _sentence(f"You sent a photo. After you said yes, I saved {_named(detail, 'bill')}"))
        return ("paid", who, _sentence(f"The payment for {_named(detail, 'bill')} was recorded"))

    if "Waiting for YES" in reason and "photo" in reason.casefold():
        return ("photo", "wait",
                _sentence(f"You sent a photo of {_named(detail, 'bill')}. I asked you to confirm before saving it"))
    if "did not save the bill from the photo" in reason:
        return ("photo", "no", "You sent a photo of a bill, then said not to save it.")

    if kind == "tool":
        tool = detail.get("tool")
        if tool == "notify_cosigner":
            return ("told", "caregiver", "I told your caregiver.")
        if tool == "flag_transaction":
            return ("flag", "flag", "I flagged a charge that did not look right.")
        if tool in {"hold_payment", "cancel_scheduled_payment"}:
            return ("stopped", tool, "I stopped a payment that was still waiting.")
        return None

    if kind == "proposal" or reason.startswith("Bill due"):
        return ("proposal", who or reason, _sentence(reason if "Bill due" in reason else
                                                     f"I sent you a payment proposal"))
    if kind == "user_approved":
        return ("approved", who, "You approved the exact payee, amount, and date.")
    if kind == "family_approved":
        return ("caregiver", who, "Your caregiver approved that same payment.")
    if kind in {"paid", "deposit"}:
        return ("paid", who or reason, _sentence(reason))
    if kind in {"held", "cancelled", "denied", "blocked"}:
        return ("stopped", who or reason, _sentence(reason))

    if reason.replace("_", " ") == reason and reason[:1].islower() and " " in reason:
        return None
    return ("other", reason, _sentence(reason))


def format_recap(events: list[dict[str, Any]]) -> list[str]:
    """A short spoken summary. Dedupes repeats. Invents no new numbers."""
    beats: list[tuple[str, str, str]] = []
    for event in events:
        beat = _beat(event)
        if beat and beat not in beats:
            beats.append(beat)
    if any(b[0] == "photo" and b[1] in {"saved", "no"} for b in beats):
        beats = [b for b in beats if not (b[0] == "photo" and b[1] == "wait")]

    proposals = [b for b in beats if b[0] == "proposal"]
    rest = [b for b in beats if b[0] != "proposal"]
    lines: list[str] = []
    if proposals:
        names = [b[1] for b in proposals if b[1] and not b[1].startswith("Bill due")]
        if len(proposals) == 1:
            lines.append(proposals[0][2])
        elif names:
            lines.append(f"I sent you payment proposals for {_join_names(names)}.")
        else:
            lines.append(f"I sent you {len(proposals)} payment proposals.")
    lines.extend(b[2] for b in rest)
    if not lines:
        return [NOTHING]
    return ["Here's what happened.\n\n" + "\n\n".join(lines)]


class AuditLog:
    def __init__(self, path: str | Path = DEFAULT_PATH, *, pause_seconds: int = PAUSE_SECONDS,
                 clock=None):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path) if str(path) != ":memory:" else None
        self.pause_seconds = pause_seconds
        self._clock = clock or _now
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_activity TEXT,
                    offered INTEGER NOT NULL DEFAULT 0,
                    awaiting INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO session (id, last_activity, offered, awaiting)
                VALUES (1, NULL, 0, 0);
                """
            )

    def close(self) -> None:
        self._db.close()

    def append(self, kind: str, reason: str, *, at: datetime | None = None,
               **detail: Any) -> dict[str, Any]:
        """Insert one row. Unknown kinds are stored as ``decision``."""
        if kind not in KINDS:
            kind = "decision"
        row = {"at": _iso(at or self._clock()), "kind": kind,
               "reason": (reason or "").strip()[:400],
               "detail": {k: v for k, v in detail.items() if v not in (None, "")}}
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO events (at, kind, reason, detail) VALUES (?, ?, ?, ?)",
                (row["at"], row["kind"], row["reason"], json.dumps(row["detail"], sort_keys=True)))
        return {"seq": cur.lastrowid, **row}

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [{"seq": r["seq"], "at": r["at"], "kind": r["kind"], "reason": r["reason"],
                 "detail": json.loads(r["detail"] or "{}")} for r in rows]

    def recap(self) -> list[str]:
        """A short spoken summary from stored rows. No new numbers."""
        return format_recap(self.events())

    def note_activity(self, now: datetime | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE session SET last_activity = ? WHERE id = 1",
                             (_iso(now or self._clock()),))

    def _session(self) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT * FROM session WHERE id = 1").fetchone()
        return dict(row) if row else {"last_activity": None, "offered": 0, "awaiting": 0}

    def offer_if_idle(self, now: datetime | None = None) -> str | None:
        """One offer after a pause, and only if there is something to recap."""
        now = now or self._clock()
        session = self._session()
        if session["offered"] or session["awaiting"]:
            return None
        last = session["last_activity"]
        if not last:
            return None
        try:
            idle = (now - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            return None
        if idle < self.pause_seconds:
            return None
        if not any(e["kind"] not in _RECAP_SKIP for e in self.events()):
            return None
        self.append("offer", "Conversation paused. Offered a recap.", at=now)
        with self._lock, self._db:
            self._db.execute("UPDATE session SET offered = 1, awaiting = 1, last_activity = ? WHERE id = 1",
                             (_iso(now),))
        return OFFER

    def take_offer(self, intent: str, *, named_payment: bool = False) -> list[str] | None:
        """If a recap is waiting, a bare yes sends it and a bare no declines."""
        if not self._session()["awaiting"]:
            return None
        if named_payment or intent not in {"yes", "no"}:
            with self._lock, self._db:
                self._db.execute("UPDATE session SET awaiting = 0 WHERE id = 1")
            return None
        with self._lock, self._db:
            self._db.execute("UPDATE session SET awaiting = 0 WHERE id = 1")
        if intent == "no":
            self.append("summary", "She did not want a recap.")
            self.append("reply", "sent to Margaret", text=DECLINE)
            self.note_activity()
            return [DECLINE]
        return self.give_recap()

    def give_recap(self) -> list[str]:
        """Send the recap now. Does not wait for the idle offer."""
        messages = self.recap()
        self.append("summary", "She asked for a recap.")
        for text in messages:
            self.append("reply", "sent to Margaret", text=text)
        with self._lock, self._db:
            self._db.execute("UPDATE session SET offered = 1, awaiting = 0 WHERE id = 1")
        self.note_activity()
        return messages


class IdleWorker:
    """Offer a recap after a pause. Does not move money."""

    def __init__(self, audit: AuditLog, send, *, interval: int = 2):
        self.audit, self.send, self.interval = audit, send, interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="audit-idle", daemon=True)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            text = self.audit.offer_if_idle()
            if text:
                try:
                    self.send(text)
                except Exception as exc:  # noqa: BLE001 - keep the ticker alive
                    log.warning("summary offer send failed: %s", type(exc).__name__)
            self.stop_event.wait(self.interval)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=3)
