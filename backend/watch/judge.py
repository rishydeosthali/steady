"""Judge: diff + rhythm + pocket + tape -> verdicts. Pure.

Code decides whether a pattern fired. Each verdict is a fact sheet plus a
consumer. Nothing here imports Nessie, the language model, or Flask.

One verdict per subject; first match wins:
  bills:      held_payment, bill_increase, bill_due
  purchases:  duplicate, pocket_shortfall, subscription, new_merchant
  account:    deposit_missing (always emitted alongside the others)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from .pocket import Pocket, compute_pocket
from .units import (BUFFER_CENTS, THRESHOLD_CENTS, WINDOW_DAYS, days_after, fmt_date, key_of,
                    money_full, money_short, next_month_on_day, record_key, weekday)

HOLD_DECISIONS = {"declined", "sarah_denied"}
DECISIONS = ("accepted", "declined", "undone", "sarah_denied")
INCREASE_FLOOR_CENTS = 500
INCREASE_PCT = Decimal("0.10")
NEAR_CENTS = 100
DUPLICATE_MAX_GAP = 2
SUBSCRIPTION_GAP = (25, 35)
SAFETY_HIDDEN = {"balance", "buffer", "left_after", "pocket", "reserved_bills", "shortfall"}


# -- diff ------------------------------------------------------------------

def fingerprint(kind: str, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "amount_cents": record.get("amount_cents"),
        "date": record["date"].isoformat() if record.get("date") else None,
        "status": record.get("status"),
    }


def frame_of(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The snapshot as a frame: fingerprint per Nessie ``_id``."""
    return {r["id"]: fingerprint(kind, r)
            for kind in ("bills", "deposits", "purchases") for r in snapshot.get(kind, [])}


@dataclass(frozen=True)
class Diff:
    added: frozenset[str] = frozenset()
    changed: frozenset[str] = frozenset()
    dropped: frozenset[str] = frozenset()

    def fresh(self, record_id: str) -> bool:
        return record_id in self.added or record_id in self.changed


def diff(frame: dict[str, dict[str, Any]], snapshot: dict[str, Any]) -> Diff:
    now = frame_of(snapshot)
    added = {i for i in now if i not in frame}
    changed = {i for i in now if i in frame and now[i] != frame[i]}
    dropped = {i for i in frame if i not in now}
    return Diff(frozenset(added), frozenset(changed), frozenset(dropped))


# -- tape ------------------------------------------------------------------

def tape_state(tape: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[tuple[str, str]]]:
    """(held payees, accepted (payee_key, payment_date) pairs), from append-only rows.

    The latest row per payee decides whether it is held. The latest row per
    payee and date decides whether that bill was accepted.
    """
    latest_payee: dict[str, dict[str, Any]] = {}
    latest_dated: dict[tuple[str, str], dict[str, Any]] = {}
    for row in tape:
        latest_payee[row["payee_key"]] = row
        if row.get("payment_date"):
            latest_dated[(row["payee_key"], row["payment_date"])] = row
    held = {k: r for k, r in latest_payee.items() if r["decision"] in HOLD_DECISIONS}
    accepted = {k for k, r in latest_dated.items() if r["decision"] == "accepted"}
    return held, accepted


# -- fact sheets -----------------------------------------------------------

def safety_view(facts: dict[str, Any]) -> dict[str, Any]:
    """Sarah never sees the balance, the pocket, or the shortfall."""
    return {k: v for k, v in facts.items() if k not in SAFETY_HIDDEN}


def _verdict(kind: str, *, notify: bool, consumer: str, dedupe_key: str, amount_cents: int | None,
             facts: dict[str, Any], subject: dict[str, Any], source: str) -> dict[str, Any]:
    if consumer == "safety":
        facts = safety_view(facts)
    return {
        "kind": kind,
        "source": source,
        "notify": notify,
        "consumer": consumer,
        "dedupe_key": dedupe_key,
        "over_threshold": amount_cents is not None and amount_cents > THRESHOLD_CENTS,
        "facts": facts,
        "subject": subject,
    }


def _subject(kind: str, record: dict[str, Any], key: str) -> dict[str, Any]:
    return {
        "type": kind,
        "id": record.get("id"),
        "key": key,
        "amount_cents": record.get("amount_cents"),
        "date": record["date"].isoformat() if record.get("date") else None,
    }


def pocket_facts(pocket: Pocket, *, merchant: str | None, when: str, clock: date,
                 purchase_date: date | None, subscription: bool) -> dict[str, Any]:
    return {
        "merchant": merchant,
        "purchase_amount": money_full(pocket.purchase_cents),
        "when": when,
        "balance": money_full(pocket.balance_cents),
        "buffer": money_full(BUFFER_CENTS),
        "left_after": money_full(pocket.left_after_cents),
        "horizon_date": fmt_date(pocket.horizon_date),
        "horizon_reason": pocket.horizon_reason,
        "horizon_label": pocket.horizon_label,
        "same_day_as_clock": purchase_date == clock,
        "reserved_bills": [
            {"payee": b.get("payee"), "amount": money_full(b["amount_cents"]),
             "date": fmt_date(b.get("date")), "weekday": weekday(b.get("date"))}
            for b in pocket.reserved
        ],
        "subscription": subscription,
    }


# -- account-level: missing deposit ---------------------------------------

def open_missing_deposits(snapshot: dict[str, Any], rhythm: dict[str, Any],
                          clock: date) -> list[dict[str, Any]]:
    """Steady monthly deposits with nothing completed or pending in their window."""
    missing = []
    for key, r in sorted(rhythm.get("deposits", {}).items()):
        if r["status"] != "steady" or r["day_of_month"] is None or not r["last_date"]:
            continue
        expected = next_month_on_day(date.fromisoformat(r["last_date"]), r["day_of_month"])
        window_end = days_after(expected, 2)
        if clock <= window_end:
            continue
        same = [d for d in snapshot.get("deposits", []) if record_key("deposits", d) == key]
        if any(d["status"] == "completed" and d.get("date") and d["date"] >= expected for d in same):
            continue
        if any(d["status"] == "pending" and d.get("date") and expected <= d["date"] <= window_end
               for d in same):
            continue
        missing.append({"key": key, "display": r["display"], "expected": expected,
                        "window_end": window_end, "usual_cents": r["median_cents"]})
    return missing


def _deposit_missing(m: dict[str, Any], source: str) -> dict[str, Any]:
    return _verdict(
        "deposit_missing", notify=True, consumer="mom",
        dedupe_key=f"deposit_missing:{m['key']}:{m['expected'].isoformat()}",
        amount_cents=None,
        facts={
            "deposit": m["display"],
            "usual_amount": money_full(m["usual_cents"]),
            "rhythm_status": "steady",
            "expected_date": fmt_date(m["expected"]),
            "expected_weekday": weekday(m["expected"]),
            "window_end": fmt_date(m["window_end"]),
            "same_day_as_clock": False,
        },
        subject={"type": "deposits", "id": None, "key": m["key"], "amount_cents": m["usual_cents"],
                 "date": m["expected"].isoformat()},
        source=source)


# -- bills -----------------------------------------------------------------

def _bill_increase(bill: dict[str, Any], r: dict[str, Any] | None) -> bool:
    if not r or r["status"] != "steady":
        return False
    pending, previous = bill.get("amount_cents"), r["previous_cents"]
    if pending is None or previous is None or pending <= previous:
        return False
    allowed = max(Decimal(INCREASE_FLOOR_CENTS), Decimal(r["median_cents"]) * INCREASE_PCT)
    return Decimal(pending - r["high_cents"]) > allowed


def _bill_facts(bill: dict[str, Any], clock: date) -> dict[str, Any]:
    return {
        "payee": bill.get("payee"),
        "amount": money_full(bill.get("amount_cents")),
        "date": fmt_date(bill.get("date")),
        "weekday": weekday(bill.get("date")),
        "same_day_as_clock": bill.get("date") == clock,
    }


def _judge_bill(bill, key, rhythm, held, accepted, clock, hold_scheduling, source):
    subject = _subject("bills", bill, key)
    amount = bill.get("amount_cents")
    if key in held:
        return _verdict(
            "held_payment", notify=True, consumer="mom",
            dedupe_key=f"held_payment:{key}:{bill['id']}", amount_cents=amount,
            facts={**_bill_facts(bill, clock), "decision": held[key]["decision"]},
            subject=subject, source=source)

    r = rhythm.get("bills", {}).get(key)
    if _bill_increase(bill, r):
        return _verdict(
            "bill_increase", notify=True, consumer="mom",
            dedupe_key=f"bill_increase:{bill['id']}:{amount}", amount_cents=amount,
            facts={
                "payee": r["display"],
                "previous": money_short(r["previous_cents"]),
                "pending": money_short(amount),
                "usual_low": money_short(r["low_cents"]),
                "usual_high": money_short(r["high_cents"]),
                "rhythm_status": r["status"],
                "date": fmt_date(bill.get("date")),
                "weekday": weekday(bill.get("date")),
                "same_day_as_clock": bill.get("date") == clock,
            },
            subject=subject, source=source)

    due = bill.get("date")
    if due is None or due > days_after(clock, WINDOW_DAYS):
        return None
    if due and (key, due.isoformat()) in accepted:
        return None
    return _verdict(
        "bill_due", notify=False, consumer="planner",
        dedupe_key=f"bill_due:{bill['id']}:{amount}:{due.isoformat()}", amount_cents=amount,
        facts={**_bill_facts(bill, clock), "days_until": (due - clock).days,
               "overdue": due < clock, "hold_scheduling": hold_scheduling},
        subject=subject, source=source)


# -- purchases ------------------------------------------------------------

def _completed_by_key(snapshot: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [p for p in snapshot.get("purchases", [])
            if p["status"] == "completed" and p.get("amount_cents") is not None
            and record_key("purchases", p) == key]


def _find_duplicate(p, same):
    best = None
    for q in same:
        if q["id"] == p["id"] or not (p.get("date") and q.get("date")):
            continue
        gap = abs((p["date"] - q["date"]).days)
        if gap <= DUPLICATE_MAX_GAP and abs(p["amount_cents"] - q["amount_cents"]) <= NEAR_CENTS:
            if best is None or gap < best[0]:
                best = (gap, q)
    return best[1] if best else None


def _find_subscription(p, same):
    others = [q for q in same if q["id"] != p["id"]]
    if len(others) != 1:
        return None
    q = others[0]
    if not (p.get("date") and q.get("date")) or q["date"] >= p["date"]:
        return None
    gap = (p["date"] - q["date"]).days
    lo, hi = SUBSCRIPTION_GAP
    if lo <= gap <= hi and abs(p["amount_cents"] - q["amount_cents"]) <= NEAR_CENTS:
        return q
    return None


def _judge_purchase(p, key, snapshot, rhythm, clock, deposit_missing, source):
    same = _completed_by_key(snapshot, key)
    pocket = compute_pocket(snapshot.get("balance_cents"), snapshot.get("bills", []),
                            snapshot.get("deposits", []), clock, p["amount_cents"], deposit_missing)
    short = pocket is not None and not pocket.holds
    subject = _subject("purchases", p, key)
    merchant = p.get("merchant") or (rhythm.get("purchases", {}).get(key) or {}).get("display")
    amount = p["amount_cents"]

    q = _find_duplicate(p, same)
    if q:
        first, second = sorted((p, q), key=lambda r: (r["date"], r["id"]))
        both_today = first["date"] == clock and second["date"] == clock
        facts = {
            "merchant": merchant,
            "amount": money_short(first["amount_cents"]),
            "second_amount": money_short(second["amount_cents"]),
            "first_date": fmt_date(first["date"]),
            "second_date": fmt_date(second["date"]),
            "same_day_as_clock": both_today,
        }
        if short:
            facts["pocket"] = pocket_facts(pocket, merchant=merchant, when="posted", clock=clock,
                                           purchase_date=p["date"], subscription=False)
        return _verdict(
            "duplicate", notify=True, consumer="mom",
            dedupe_key=f"duplicate:{key}:{first['amount_cents']}:{first['date'].isoformat()}",
            amount_cents=amount, facts=facts, subject=subject, source=source)

    sub = _find_subscription(p, same)
    if short:
        return _verdict(
            "pocket_shortfall", notify=True, consumer="mom",
            dedupe_key=f"pocket_shortfall:{p['id']}", amount_cents=amount,
            facts=pocket_facts(pocket, merchant=merchant, when="posted", clock=clock,
                               purchase_date=p.get("date"), subscription=sub is not None),
            subject=subject, source=source)

    if sub:
        return _verdict(
            "subscription", notify=True, consumer="mom",
            dedupe_key=f"subscription:{key}:{amount}", amount_cents=amount,
            facts={
                "merchant": merchant,
                "amount": money_short(amount),
                "previous_date": fmt_date(sub["date"]),
                "date": fmt_date(p.get("date")),
                "gap_days": (p["date"] - sub["date"]).days,
                "same_day_as_clock": p.get("date") == clock,
            },
            subject=subject, source=source)

    if len(same) == 1:
        return _verdict(
            "new_merchant", notify=False, consumer="memory_only",
            dedupe_key=f"new_merchant:{key}:{p['id']}", amount_cents=amount,
            facts={"merchant": merchant, "amount": money_full(amount), "date": fmt_date(p.get("date")),
                   "same_day_as_clock": p.get("date") == clock},
            subject=subject, source=source)
    return None


# -- the pass ---------------------------------------------------------------

@dataclass
class Judgment:
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    missing_keys: set[str] = field(default_factory=set)

    @property
    def hold_scheduling(self) -> bool:
        return bool(self.missing_keys)


def judge(snapshot: dict[str, Any], rhythm: dict[str, Any], changes: Diff,
          tape: list[dict[str, Any]], clock: date, first_look: bool) -> Judgment:
    """Every verdict this pull supports. Dedupe against history happens in run."""
    source = snapshot.get("source", "nessie")
    out = Judgment()
    seen: set[str] = set()

    def add(v):
        if v and v["dedupe_key"] not in seen:
            seen.add(v["dedupe_key"])
            out.verdicts.append(v)

    missing = open_missing_deposits(snapshot, rhythm, clock)
    for m in missing:
        v = _deposit_missing(m, source)
        out.missing_keys.add(v["dedupe_key"])
        add(v)

    held, accepted = tape_state(tape)
    pending = sorted((b for b in snapshot.get("bills", []) if b["status"] == "pending"),
                     key=lambda b: (b.get("date") or date.max, b["id"]))
    for bill in pending:
        add(_judge_bill(bill, record_key("bills", bill), rhythm, held, accepted, clock,
                        bool(missing), source))

    # First look is a baseline for purchases: history never notifies.
    if not first_look:
        fresh = sorted((p for p in snapshot.get("purchases", [])
                        if p["status"] == "completed" and p.get("amount_cents") is not None
                        and changes.fresh(p["id"])),
                       key=lambda p: (p.get("date") or date.min, p["id"]))
        for p in fresh:
            key = record_key("purchases", p)
            if key:
                add(_judge_purchase(p, key, snapshot, rhythm, clock, bool(missing), source))
    return out


@dataclass(frozen=True)
class CheckResult:
    verdict: dict[str, Any] | None
    pocket: Pocket | None


def judge_contemplated(snapshot: dict[str, Any], rhythm: dict[str, Any], tape: list[dict[str, Any]],
                       clock: date, merchant: str, amount_cents: int) -> CheckResult:
    """A "what if" purchase. Never enters the frame, the rhythm, or the dedupe store."""
    source = snapshot.get("source", "nessie")
    key = key_of(merchant)
    subject = {"type": "contemplated", "id": None, "key": key, "amount_cents": amount_cents,
               "date": None}
    held, _ = tape_state(tape)
    if key in held:
        return CheckResult(_verdict(
            "held_payment", notify=True, consumer="mom",
            dedupe_key=f"held_payment:{key}:contemplate", amount_cents=amount_cents,
            facts={"payee": merchant, "amount": money_full(amount_cents), "date": None,
                   "weekday": None, "same_day_as_clock": False, "decision": held[key]["decision"]},
            subject=subject, source=source), None)

    missing = open_missing_deposits(snapshot, rhythm, clock)
    pocket = compute_pocket(snapshot.get("balance_cents"), snapshot.get("bills", []),
                            snapshot.get("deposits", []), clock, amount_cents, bool(missing))
    if pocket is None or pocket.holds:
        return CheckResult(None, pocket)
    facts = pocket_facts(pocket, merchant=merchant, when="contemplated", clock=clock,
                         purchase_date=None, subscription=False)
    return CheckResult(_verdict(
        "pocket_shortfall", notify=True, consumer="mom",
        dedupe_key=f"pocket_shortfall:contemplate:{amount_cents}", amount_cents=amount_cents,
        facts=facts, subject=subject, source=source), pocket)


def holds_facts(pocket: Pocket, merchant: str, clock: date) -> dict[str, Any]:
    """Facts for answering a "what if" that fits. Not a verdict."""
    return pocket_facts(pocket, merchant=merchant, when="contemplated", clock=clock,
                        purchase_date=None, subscription=False)
