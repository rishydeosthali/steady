"""Pipeline, ledger, wording, the contemplate branch, and the /watch routes."""

import copy
from datetime import date

import pytest

from backend.agent import reply
from backend.agent.intent import normalize, parse_amount_cents
from backend.agent.loop import Agent
from backend.app import create_app
from backend.watch.demo import (LIVE_CLOCK, bill, deposit_missing_shape, live_shape, purchase,
                                snapshot)
from backend.watch.ledger import DecisionError, Ledger
from backend.watch.run import Watcher
from backend.watch.wording import fallback, grounded, word
from tests.conftest import FakeLLM, FakeNessie
from tests.test_app import InlineExecutor, RecordingTwilio

OCT6 = date(2026, 10, 6)


class Source:
    """A pull that returns whatever snapshot the test sets."""

    def __init__(self, shape):
        self.shape = shape
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return copy.deepcopy(self.shape)


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    yield led
    led.close()


def watcher(ledger, shape, **kw):
    return Watcher(ledger, Source(shape), today=lambda: LIVE_CLOCK, **kw)


# -- pipeline ----------------------------------------------------------------

def test_first_pass_then_identical_pass_emits_nothing_new(ledger):
    w = watcher(ledger, live_shape())
    first = w.run(LIVE_CLOCK)
    assert [v["kind"] for v in first] == ["bill_due", "bill_due"]
    assert w.run(LIVE_CLOCK) == []
    queue = ledger.events(consumer="planner", delivered=False)
    assert [e["facts"]["payee"] for e in queue] == ["Austin Energy", "Water Co"]
    assert ledger.load_rhythm()["purchases"]["heb"]["status"] == "uneven"


def test_deleting_the_ledger_makes_the_next_pass_a_first_look(tmp_path):
    path = tmp_path / "ledger.sqlite"
    led = Ledger(path)
    w = watcher(led, live_shape())
    w.run(LIVE_CLOCK)
    w._pull.shape["purchases"].append(purchase("p-new", "Joann", "22.00", "2026-10-24"))
    assert [v["kind"] for v in w.run(LIVE_CLOCK)] == ["new_merchant"]
    led.close()
    path.unlink()
    led2 = Ledger(path)
    assert [v["kind"] for v in watcher(led2, w._pull.shape).run(LIVE_CLOCK)] == ["bill_due", "bill_due"]
    led2.close()


def test_mom_verdict_is_worded_and_waits_when_no_session(ledger):
    shape = live_shape()
    w = watcher(ledger, shape)
    w.run(LIVE_CLOCK)
    shape["purchases"] += [purchase("d1", "dentist", "120.00", "2026-10-24"),
                           purchase("d2", "dentist", "120.00", "2026-10-24")]
    sent = []
    w._mom_sink = lambda v, t: False
    out = w.run(LIVE_CLOCK)
    assert [v["kind"] for v in out] == ["duplicate"]
    assert out[0]["text"] == "There are two $120 dentist charges today. Were you expecting both?"
    assert ledger.events(consumer="mom", delivered=False)[0]["kind"] == "duplicate"
    assert sent == []


def test_mom_sink_delivers_inside_a_session(ledger):
    shape = deposit_missing_shape()
    sent = []
    w = Watcher(ledger, Source(shape), mom_sink=lambda v, t: sent.append(t) or True)
    w.run(OCT6)
    assert sent == [fallback(ledger.events(consumer="mom")[0])]
    assert ledger.events(consumer="mom")[0]["delivered"] is True


def test_missing_deposit_sets_and_clears_hold_scheduling_on_the_queue(ledger):
    shape = deposit_missing_shape(bills=[bill("b-ae", "Austin Energy", "120.00", "2026-10-15")])
    w = Watcher(ledger, Source(shape))
    w.run(OCT6)
    assert ledger.events(consumer="planner")[0]["facts"]["hold_scheduling"] is True
    w._pull.shape["deposits"].append(
        {"id": "d-oct", "description": "Social Security", "amount_cents": 187400,
         "date": date(2026, 10, 7), "status": "completed"})
    assert w.run(date(2026, 10, 8)) == []                      # cleared quietly
    assert ledger.events(consumer="planner")[0]["facts"]["hold_scheduling"] is False
    assert ledger.events(consumer="mom")[0]["resolved"] is True


def test_held_payee_cannot_come_back_as_bill_due(ledger):
    ledger.record_decision("Roofer", "sarah_denied")
    shape = snapshot("2213.00", bills=[bill("b-roof", "Roofer", "2400.00", "2026-10-30")])
    out = watcher(ledger, shape).run(LIVE_CLOCK)
    assert [v["kind"] for v in out] == ["held_payment"]
    assert ledger.events(consumer="planner") == []


def test_contemplated_check_stores_nothing_and_answers_twice(ledger):
    w = watcher(ledger, live_shape())
    a = w.check("Dentist", 190000, LIVE_CLOCK)
    b = w.check("Dentist", 190000, LIVE_CLOCK)
    assert a["text"] == b["text"] == "This would leave $147.75 after your upcoming bills."
    assert ledger.events() == [] and ledger.load_frame() == {}


def test_contemplated_fit_answers_with_what_is_left(ledger):
    r = watcher(ledger, live_shape()).check("H-E-B", 4000, LIVE_CLOCK)
    assert r["verdict"] is None
    assert r["text"] == "That fits. It would leave $2,007.75 after your upcoming bills."


# -- tape ----------------------------------------------------------------------

def test_tape_is_append_only_and_validated(ledger):
    ledger.record_decision("Austin Energy", "accepted", amount_cents=12000, payment_date="2026-10-31")
    ledger.record_decision("Austin Energy", "undone", payment_date="2026-10-31")
    rows = ledger.tape()
    assert [r["decision"] for r in rows] == ["accepted", "undone"]
    assert rows[0]["payee_key"] == "austin energy"
    with pytest.raises(DecisionError):
        ledger.record_decision("Roofer", "maybe")
    with pytest.raises(DecisionError):
        ledger.record_decision("", "declined")
    with pytest.raises(DecisionError):
        ledger.record_decision("Roofer", "declined", amount_cents=12.5)


# -- wording -------------------------------------------------------------------

def shortfall_sheet():
    from backend.watch.judge import judge_contemplated
    from backend.watch.rhythm import build_rhythm
    shape = live_shape()
    return judge_contemplated(shape, build_rhythm(shape), [], LIVE_CLOCK, "Dentist", 190000).verdict


def test_rewrite_with_sheet_values_and_weekday_is_kept():
    sheet = shortfall_sheet()
    text = "That would leave $147.75 once Austin Energy is paid on Saturday, Oct 31."
    assert word(sheet, FakeLLM(reply={"text": text})) == text


@pytest.mark.parametrize("bad", [
    "That would leave $150.00 after your bills.",                 # invented dollar
    "That would leave $147.75 after Nov 12.",                     # date not in the sheet
    "That would leave $147.75. Austin Energy is due Friday.",     # weekday not in the sheet
    "That would leave $147.75 tomorrow.",
    "Today that would leave $147.75.",                            # same_day_as_clock is false
    "You usually have more. That would leave $147.75.",           # no steady rhythm
])
def test_ungrounded_rewrite_falls_back(bad):
    sheet = shortfall_sheet()
    assert not grounded(bad, sheet["facts"])
    assert word(sheet, FakeLLM(reply={"text": bad})) == fallback(sheet)


def test_today_is_allowed_only_when_the_sheet_says_so():
    assert grounded("Two charges today.", {"same_day_as_clock": True})
    assert not grounded("Two charges today.", {"same_day_as_clock": False})


def test_existing_reply_weekday_ban_is_unchanged():
    from backend.agent.reply import is_grounded
    assert not is_grounded(["Due Saturday."], {"x": "Saturday"})


# -- contemplate intent ------------------------------------------------------

def test_amount_comes_from_code_not_the_model():
    assert parse_amount_cents("What if I spend $1,900 at the dentist?") == 190000
    assert parse_amount_cents("Can I afford $40 at H-E-B?") == 4000
    assert parse_amount_cents("could I spend 25 dollars at CVS") == 2500
    assert parse_amount_cents("what if I go to the dentist") is None
    assert parse_amount_cents("$40 or $50 at H-E-B?") is None


def test_contemplate_keeps_merchant_and_confidence_floor():
    p = normalize({"intent": "contemplate", "confidence": 0.9, "needs": [], "restatement": "",
                   "merchant": "the dentist"})
    assert p.intent == "contemplate" and p.merchant == "the dentist"
    assert normalize({"intent": "contemplate", "confidence": 0.4, "merchant": "x"}).intent == "unclear"


def contemplate_llm(merchant):
    return FakeLLM(intent={"intent": "contemplate", "confidence": 0.95, "needs": [],
                           "restatement": "", "merchant": merchant},
                   reply={"text": ""})


def test_contemplate_on_whatsapp_uses_live_bills_and_stores_nothing(ledger):
    w = watcher(ledger, live_shape())
    nessie = FakeNessie()
    agent = Agent(contemplate_llm("dentist"), nessie, "Margaret", watcher=w)
    out = agent.respond("What if I spend $1,900 at the dentist?")
    assert out == ["This would leave $147.75 after your upcoming bills."]
    assert w._pull.calls == 1 and ledger.events() == []


def test_contemplate_without_amount_reasks_and_reads_nothing(ledger):
    w = watcher(ledger, live_shape())
    out = Agent(contemplate_llm("dentist"), FakeNessie(), "Margaret", watcher=w).respond(
        "What if I go to the dentist?")
    assert out == [reply.ASK_AMOUNT] and w._pull.calls == 0


def test_contemplate_merchant_must_appear_in_her_text(ledger):
    w = watcher(ledger, live_shape())
    out = Agent(contemplate_llm("Austin Energy"), FakeNessie(), "Margaret", watcher=w).respond(
        "What if I spend $1,900?")
    assert out == [reply.ASK_PAYEE] and w._pull.calls == 0


def test_pay_request_still_does_not_pay(ledger):
    llm = FakeLLM(intent={"intent": "pay_request", "confidence": 0.99, "needs": [],
                          "restatement": "", "merchant": ""})
    w = watcher(ledger, live_shape())
    assert Agent(llm, FakeNessie(), "Margaret", watcher=w).respond("Pay Austin Energy") == [reply.PAY_NOT_ON]
    assert w._pull.calls == 0


# -- staged WhatsApp demo -----------------------------------------------------------

STAGED = {
    "duplicate": "There are two $120 Dentist charges today. Were you expecting both?",
    "subscription": "StreamBox charged $14.99 again. This may be a monthly subscription.",
    "bill_increase": "Your Internet bill increased from $65 to $85.",
    "shortfall": "This leaves $147.75 after your upcoming bills.",
    "deposit_missing": "Your usual deposit hasn't appeared yet. Let's check before scheduling payments.",
    "held": "You already put this payment on hold. It's still on hold.",
}


@pytest.mark.parametrize("name", sorted(STAGED))
def test_staged_scenario_is_silent_then_texts_once(name, tmp_path):
    from backend.watch import stage
    sent = []
    sink = lambda v, t: sent.append(t) or True  # noqa: E731
    path = tmp_path / "demo.sqlite"
    base = stage.run_step(name, "baseline", mom_sink=sink, path=path)
    assert sent == [] and base["memory"]
    event = stage.run_step(name, "event", mom_sink=sink, path=path)
    assert sent == [STAGED[name]]
    assert [v["sent"] for v in event["verdicts"] if v["consumer"] == "mom"] == [True]
    assert stage.run_step(name, "baseline", mom_sink=sink, path=path)["verdicts"] == base["verdicts"]


def test_staged_demo_never_touches_the_live_ledger():
    from backend.watch import stage
    from backend.watch.ledger import DEFAULT_PATH
    assert stage.DEMO_PATH != DEFAULT_PATH and stage.DEMO_PATH.parent == DEFAULT_PATH.parent


# -- routes ---------------------------------------------------------------------

def make_client(settings, ledger, shape=None):
    w = watcher(ledger, shape or live_shape())
    app = create_app(settings, agent=Agent(FakeLLM(), FakeNessie(), "Margaret"),
                     twilio=RecordingTwilio(settings), executor=InlineExecutor(), watcher=w)
    return app.test_client()


def test_watch_run_and_planner_queue(settings, ledger):
    c = make_client(settings, ledger)
    r = c.post("/watch/run", json={"date": "2026-10-24"})
    assert r.status_code == 200 and len(r.get_json()["verdicts"]) == 2
    q = c.get("/watch/events?consumer=planner&delivered=false").get_json()["events"]
    assert [e["kind"] for e in q] == ["bill_due", "bill_due"]
    assert all(e["delivered"] is False for e in q)


def test_watch_decisions_appends_and_validates(settings, ledger):
    c = make_client(settings, ledger)
    ok = c.post("/watch/decisions", json={"payee": "Roofer", "decision": "sarah_denied", "amount": "2400"})
    assert ok.status_code == 201 and ok.get_json()["tape_row"]["amount_cents"] == 240000
    assert c.post("/watch/decisions", json={"payee": "Roofer", "decision": "maybe"}).status_code == 400


def test_watch_check_does_not_store(settings, ledger):
    c = make_client(settings, ledger)
    r = c.post("/watch/check", json={"merchant": "Dentist", "amount": "1900", "date": "2026-10-24"})
    assert r.get_json()["text"] == "This would leave $147.75 after your upcoming bills."
    assert ledger.events() == []


def test_watch_routes_refuse_requests_relayed_by_the_tunnel(settings, ledger):
    c = make_client(settings, ledger)
    for headers in ({"X-Forwarded-For": "1.2.3.4"}, {"Cf-Connecting-Ip": "1.2.3.4"}):
        assert c.post("/watch/run", headers=headers).status_code == 403
        assert c.post("/watch/decisions", headers=headers,
                      json={"payee": "Roofer", "decision": "declined"}).status_code == 403
    assert ledger.tape() == []


def test_watch_routes_without_a_watcher_are_unavailable(settings):
    app = create_app(settings, agent=Agent(FakeLLM(), FakeNessie(), "Margaret"),
                     twilio=RecordingTwilio(settings), executor=InlineExecutor())
    assert app.test_client().post("/watch/run").status_code == 503
