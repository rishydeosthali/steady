"""Stage a Watcher scenario on WhatsApp with synthetic data.

    python -m backend.watch.stage               # list scenarios
    python -m backend.watch.stage duplicate     # baseline, press Enter, the new event

Each scenario runs the real pipeline twice against a separate memory file,
``data/demo.sqlite``. The first pass memorizes a baseline account and stays
silent. The second pass sees one new record and texts Mom through the running
server. Live Nessie and ``data/ledger.sqlite`` are never touched.

The server must be running, and Mom must have texted in the last 24 hours.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .demo import LIVE_CLOCK, bill, deposit, deposit_missing_shape, live_shape, purchase
from .ledger import DEFAULT_PATH, Ledger
from .rhythm import build_rhythm
from .run import MomSink, Watcher
from .units import money_full, money_short

DEMO_PATH = DEFAULT_PATH.with_name("demo.sqlite")
STEPS = ("baseline", "event")


@dataclass(frozen=True)
class Scenario:
    title: str
    event: str
    baseline: Callable[[], dict[str, Any]]
    after: Callable[[], dict[str, Any]]
    baseline_clock: date = LIVE_CLOCK
    event_clock: date = LIVE_CLOCK
    decision: tuple[str, str] | None = None


def _with(shape: dict[str, Any], bills=(), purchases=()) -> dict[str, Any]:
    shape["bills"] += list(bills)
    shape["purchases"] += list(purchases)
    return shape


def _internet_history() -> list[dict[str, Any]]:
    return [bill("b-net-aug", "Internet", "65.00", "2026-08-15", "completed"),
            bill("b-net-sep", "Internet", "65.00", "2026-09-15", "completed")]


DENTIST_1 = purchase("p-dent-1", "Dentist", "120.00", "2026-10-24")
DENTIST_2 = purchase("p-dent-2", "Dentist", "120.00", "2026-10-24")
STREAM_SEP = purchase("p-stream-sep", "StreamBox", "14.99", "2026-09-24")
STREAM_OCT = purchase("p-stream-oct", "StreamBox", "14.99", "2026-10-24")
ROOFER = bill("b-roof", "Roofer", "2400.00", "2026-10-30")

SCENARIOS: dict[str, Scenario] = {
    "duplicate": Scenario(
        "Charged twice", "A second $120 dentist charge posts today",
        lambda: _with(live_shape(), purchases=[DENTIST_1]),
        lambda: _with(live_shape(), purchases=[DENTIST_1, DENTIST_2])),
    "subscription": Scenario(
        "A charge that came back", "StreamBox charges $14.99 again, a month after the first",
        lambda: _with(live_shape(), purchases=[STREAM_SEP]),
        lambda: _with(live_shape(), purchases=[STREAM_SEP, STREAM_OCT])),
    "bill_increase": Scenario(
        "A bill went up", "The new internet bill arrives at $85 (it was $65 twice)",
        lambda: _with(live_shape(), bills=_internet_history()),
        lambda: _with(live_shape(), bills=_internet_history()
                      + [bill("b-net-nov", "Internet", "85.00", "2026-11-15")])),
    "shortfall": Scenario(
        "A purchase that leaves her short", "A $1,900 dentist charge posts",
        live_shape,
        lambda: _with(live_shape(), purchases=[purchase("p-big", "Dentist", "1900.00", "2026-10-24")])),
    "deposit_missing": Scenario(
        "Paycheck didn't come", "Time passes to Oct 6. No Social Security deposit yet",
        deposit_missing_shape, deposit_missing_shape,
        baseline_clock=date(2026, 10, 2), event_clock=date(2026, 10, 6)),
    "held": Scenario(
        "A payment she put on hold returns", "A $2,400 Roofer bill shows up again",
        live_shape, lambda: _with(live_shape(), bills=[ROOFER]),
        decision=("Roofer", "sarah_denied")),
}


def memory_lines(rhythm: dict[str, Any]) -> list[str]:
    """What the baseline taught it, in plain words."""
    lines = []
    for kind in ("deposits", "bills", "purchases"):
        for r in rhythm.get(kind, {}).values():
            if r["completed_count"] < 2:
                continue
            if r["status"] == "steady":
                when = f" around the {_ordinal(r['day_of_month'])}" if r["day_of_month"] else ""
                lines.append(f"{r['display']}: steady, {money_full(r['median_cents'])}{when}")
            else:
                lines.append(f"{r['display']}: varies, {money_short(r['low_cents'])} to "
                             f"{money_short(r['high_cents'])}")
    return lines


def _ordinal(day: int) -> str:
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def run_step(name: str, step: str, *, llm=None, mom_sink: MomSink | None = None,
             path: Path = DEMO_PATH) -> dict[str, Any]:
    """One step of a scenario. ``baseline`` starts from an empty memory."""
    scenario = SCENARIOS[name]
    if step == "baseline" and path.exists():
        path.unlink()
    shape = scenario.baseline() if step == "baseline" else scenario.after()
    clock = scenario.baseline_clock if step == "baseline" else scenario.event_clock
    sent: list[str] = []

    def sink(verdict: dict[str, Any], text: str) -> bool:
        ok = bool(mom_sink and mom_sink(verdict, text))
        if ok:
            sent.append(verdict["dedupe_key"])
        return ok

    ledger = Ledger(path)
    try:
        verdicts = Watcher(ledger, lambda: shape, llm=llm, mom_sink=sink).run(clock)
        if step == "baseline" and scenario.decision:
            ledger.record_decision(*scenario.decision)
    finally:
        ledger.close()
    return {
        "scenario": name, "title": scenario.title, "step": step, "clock": clock.isoformat(),
        "event": scenario.event if step == "event" else None,
        "memory": memory_lines(build_rhythm(shape)) if step == "baseline" else [],
        "decision": scenario.decision if step == "baseline" else None,
        "verdicts": [{"kind": v["kind"], "consumer": v["consumer"], "text": v["text"],
                      "sent": v["dedupe_key"] in sent} for v in verdicts],
    }


# -- CLI: drives the running server -------------------------------------------

SESSION_HINT = (
    "WhatsApp will not deliver yet. From Mom's phone, text the sandbox \"hi\", "
    "wait for a reply, then run this again from C:\\Projects\\steady\\steady."
)


def _get(base: str, path: str) -> dict[str, Any]:
    import requests

    try:
        r = requests.get(f"{base}{path}", timeout=10)
    except requests.RequestException as exc:
        raise SystemExit(f"Cannot reach the server at {base}. Start it first: "
                         f".\\.venv\\Scripts\\python.exe -m backend.app\n({exc})") from None
    if r.status_code != 200:
        raise SystemExit(f"Server said {r.status_code}: {r.text.strip()[:200]}")
    return r.json()


def _post(base: str, name: str, step: str) -> dict[str, Any]:
    import requests

    try:
        r = requests.post(f"{base}/watch/demo/{name}/{step}", timeout=60)
    except requests.RequestException as exc:
        raise SystemExit(f"Cannot reach the server at {base}: {exc}") from None
    if r.status_code != 200:
        raise SystemExit(f"Server said {r.status_code}: {r.text.strip()[:200]}")
    return r.json()


def _print(result: dict[str, Any]) -> None:
    if result["step"] == "baseline":
        print(f"\n[1] Baseline memorized (clock {result['clock']}).")
        for line in result["memory"]:
            print(f"    remembers  {line}")
        if result["decision"]:
            print(f"    on record  {result['decision'][0]} payment put on hold")
        mom = [v for v in result["verdicts"] if v["consumer"] == "mom"]
        print(f"    texts to Mom: {len(mom)}")
        return
    print(f"\n[2] New: {result['event']} (clock {result['clock']}).")
    if not result["verdicts"]:
        print("    nothing worth a text")
    for v in result["verdicts"]:
        if v["consumer"] == "mom":
            status = "SENT to WhatsApp" if v["sent"] else f"NOT sent. {SESSION_HINT}"
            print(f"    {v['kind']}: \"{v['text']}\"  -> {status}")
        else:
            print(f"    {v['kind']}: silent, for {v['consumer']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage a Watcher alert on WhatsApp.")
    parser.add_argument("scenario", nargs="?", choices=sorted(SCENARIOS))
    parser.add_argument("--no-pause", action="store_true", help="Skip the Enter prompt.")
    parser.add_argument("--server", default="http://127.0.0.1:5000")
    args = parser.parse_args(argv)
    if not args.scenario:
        for name, s in SCENARIOS.items():
            print(f"{name:16} {s.title}: {s.event}")
        return 0

    session = _get(args.server, "/watch/session")
    if not session.get("open"):
        print(SESSION_HINT)
        return 2

    print(f"== {SCENARIOS[args.scenario].title} ==")
    _print(_post(args.server, args.scenario, "baseline"))
    if not args.no_pause:
        input(f"\nPress Enter to add: {SCENARIOS[args.scenario].event} ")
    _print(_post(args.server, args.scenario, "event"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
