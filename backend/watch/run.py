"""One watch pass: pull, diff, rebuild rhythm, judge, dedupe, store, hand off.

    python -m backend.watch.run [--date YYYY-MM-DD]

No background thread. The demo fires a pass on purpose. Read-only against
Nessie. Nothing here picks a payment date or moves money; ``bill_due``
verdicts are stored for the planner with ``delivered: false``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from datetime import date
from typing import Any, Callable

from .judge import diff, frame_of, holds_facts, judge, judge_contemplated
from .ledger import Ledger
from .rhythm import build_rhythm
from .units import bill_record, deposit_record, purchase_record, to_cents
from .wording import holds_sentence, word

log = logging.getLogger(__name__)

Pull = Callable[[], dict[str, Any]]
MomSink = Callable[[dict[str, Any], str], bool]


def nessie_puller(nessie, customer_first_name: str) -> Pull:
    """Live reads through the existing client. Discovers ids every pull."""
    merchant_names: dict[str, str | None] = {}

    def merchant_name(merchant_id: str | None) -> str | None:
        if not merchant_id:
            return None
        if merchant_id not in merchant_names:
            from ..nessie.client import NessieError
            try:
                merchant_names[merchant_id] = nessie.get_merchant(merchant_id).get("name") or None
            except NessieError as exc:
                log.warning("merchant lookup failed: %s", exc)
                return None
        return merchant_names[merchant_id]

    def pull() -> dict[str, Any]:
        account_id = nessie.resolve(customer_first_name)["_id"]
        return {
            "source": "nessie",
            "balance_cents": to_cents(nessie.get_balance(account_id)),
            "bills": [bill_record(b) for b in nessie.get_bills(account_id)],
            "deposits": [deposit_record(d) for d in nessie.get_deposits(account_id)],
            "purchases": [purchase_record(p, merchant_name(p.get("merchant_id")))
                          for p in nessie.get_purchases(account_id)],
        }

    return pull


class Watcher:
    def __init__(self, ledger: Ledger, pull: Pull, *, llm=None, mom_sink: MomSink | None = None,
                 today: Callable[[], date] = date.today):
        self.ledger = ledger
        self._pull = pull
        self._llm = llm
        self._mom_sink = mom_sink
        self._today = today
        self._lock = threading.Lock()

    def run(self, clock: date | None = None) -> list[dict[str, Any]]:
        """One pass. Returns only verdicts not emitted before."""
        clock = clock or self._today()
        with self._lock:
            snapshot = self._pull()                                   # 1 pull
            frame = self.ledger.load_frame()
            first_look = not frame
            changes = diff(frame, snapshot)                           # 2 diff
            if changes.dropped:
                log.info("watch: %d record(s) dropped since the last frame", len(changes.dropped))
            rhythm = build_rhythm(snapshot)                           # 3 rhythm
            judgment = judge(snapshot, rhythm, changes, self.ledger.tape(), clock,
                             first_look)                              # 4 judge
            fresh = [v for v in judgment.verdicts
                     if not self.ledger.seen(v["dedupe_key"])]        # 5 dedupe

            self.ledger.save_frame(frame_of(snapshot))                # 6 store
            self.ledger.save_rhythm(rhythm)
            self.ledger.resolve_missing(judgment.missing_keys)
            out = []
            for v in fresh:
                text = word(v, self._llm) if v["notify"] else None    # 7 word / hand off
                self.ledger.store_verdict(v, text)
                if text and v["consumer"] == "mom" and self._mom_sink:
                    if self._mom_sink(v, text):
                        self.ledger.mark_delivered(v["dedupe_key"])
                out.append({**v, "text": text})
            self.ledger.set_hold_scheduling(judgment.hold_scheduling)
        log.info("watch pass %s: %d new verdict(s), first_look=%s", clock.isoformat(), len(out),
                 first_look)
        return out

    def check(self, merchant: str, amount_cents: int, clock: date | None = None) -> dict[str, Any]:
        """A "what if" purchase against live reads. Stores nothing."""
        clock = clock or self._today()
        snapshot = self._pull()
        result = judge_contemplated(snapshot, build_rhythm(snapshot), self.ledger.tape(), clock,
                                    merchant, amount_cents)
        if result.verdict:
            return {"verdict": result.verdict, "text": word(result.verdict, self._llm)}
        if result.pocket is None:
            return {"verdict": None, "text": None, "facts": None}
        facts = holds_facts(result.pocket, merchant, clock)
        return {"verdict": None, "text": holds_sentence(facts), "facts": facts}


def build_watcher(settings, *, llm=None, mom_sink: MomSink | None = None,
                  ledger: Ledger | None = None) -> Watcher:
    from ..nessie.client import NessieClient

    nessie = NessieClient(settings.nessie_base_url, settings.nessie_api_key)
    return Watcher(ledger or Ledger(), nessie_puller(nessie, settings.customer_first_name),
                   llm=llm, mom_sink=mom_sink)


def run_watch(clock: date | None = None, *, watcher: Watcher | None = None) -> list[dict[str, Any]]:
    """One live pass with the default ledger, or the given watcher."""
    if watcher is None:
        from ..config import load_settings
        watcher = build_watcher(load_settings())
    return watcher.run(clock)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Watcher pass against live Nessie.")
    parser.add_argument("--date", help="Clock date, YYYY-MM-DD. Default: today.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("urllib3", "httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from ..config import ConfigError
    from ..nessie.client import NessieError
    try:
        verdicts = run_watch(date.fromisoformat(args.date) if args.date else None)
    except ConfigError as exc:
        print(f"Stopping: {exc}", file=sys.stderr)
        return 1
    except NessieError as exc:
        print(f"Nessie read failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(verdicts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
