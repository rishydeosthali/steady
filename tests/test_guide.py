"""LLM tool backup: reply, email the caregiver, flag, hold/cancel only known ids."""

from backend.agent import reply
from backend.agent.guide import guide
from backend.agent.loop import Agent
from backend.watch.ledger import Ledger
from backend.watch.run import Watcher
from tests.conftest import FakeLLM, FakeNessie
from tests.test_loop import intent


DUPLICATE = {
    "kind": "duplicate",
    "source": "nessie",
    "notify": True,
    "consumer": "mom",
    "dedupe_key": "duplicate:dentist:12000:2026-10-24",
    "over_threshold": False,
    "facts": {
        "merchant": "Dentist",
        "amount": "$120",
        "second_amount": "$120",
        "first_date": "Oct 24",
        "second_date": "Oct 24",
        "same_day_as_clock": True,
    },
    "subject": {"type": "purchases", "id": "p-dent-2", "key": "dentist"},
}


class Mailer:
    def __init__(self):
        self.notes = []

    def notify(self, reason, details, urgency="soon"):
        self.notes.append({"reason": reason, "details": details, "urgency": urgency})


class Payments:
    def __init__(self):
        self.mailer = Mailer()
        self.held = []
        self.cancelled = []

    def handle(self, body, intent, sid):
        return None

    def snapshot_jobs(self):
        return [{"id": "AB12CD34", "payee": "Austin Energy", "amount": "$120.00",
                 "date": "2026-10-25", "status": "scheduled"}]

    def hold_job(self, job_id):
        self.held.append(job_id)
        return True

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return True


def watching(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.store_verdict(DUPLICATE, "There are two $120 Dentist charges today. Were you expecting both?")
    ledger.mark_delivered(DUPLICATE["dedupe_key"])
    return Watcher(ledger, lambda: {"source": "test", "balance_cents": 221300, "bills": [],
                                    "deposits": [], "purchases": []})


def test_no_after_duplicate_emails_caregiver_even_if_llm_is_down(tmp_path):
    watcher = watching(tmp_path)
    payments = Payments()
    llm = FakeLLM(intent=intent("no"))
    out = Agent(llm, FakeNessie(), "Margaret", payments=payments, watcher=watcher).respond("No", message_id="SM-no")
    assert "Dentist" in out[0] and "$120" in out[0]
    assert "caregiver" in out[0].lower()
    assert payments.mailer.notes
    note = payments.mailer.notes[0]
    assert note["details"]["merchant"] == "Dentist"
    assert note["details"]["amount"] == "$120"
    assert "balance" not in note["details"]
    assert watcher.ledger.flags()[0]["dedupe_key"] == DUPLICATE["dedupe_key"]


def test_llm_tools_reply_and_cannot_hold_unknown_payment(tmp_path):
    watcher = watching(tmp_path)
    payments = Payments()
    tools = {"actions": [
        {"tool": "reply_to_user",
         "text": "Okay. I told your caregiver about the two $120 Dentist charges on Oct 24.",
         "reason": "", "details": "", "urgency": "", "id": "", "minutes": 0, "message": ""},
        {"tool": "notify_cosigner", "text": "", "reason": "unexpected duplicate",
         "details": "", "urgency": "now", "id": DUPLICATE["dedupe_key"], "minutes": 0, "message": ""},
        {"tool": "flag_transaction", "text": "", "reason": "not expected", "details": "",
         "urgency": "", "id": DUPLICATE["dedupe_key"], "minutes": 0, "message": ""},
        {"tool": "hold_payment", "text": "", "reason": "", "details": "", "urgency": "",
         "id": "not-a-job", "minutes": 0, "message": ""},
        {"tool": "schedule_followup", "text": "", "reason": "", "details": "", "urgency": "",
         "id": "", "minutes": 30, "message": "Checking back about the Dentist charges."},
    ]}
    llm = FakeLLM(intent=intent("no"), tools=tools)
    out = Agent(llm, FakeNessie(), "Margaret", payments=payments, watcher=watcher).respond("No")
    assert "Dentist" in out[0] and "$120" in out[0] and "caregiver" in out[0].lower()
    assert payments.held == []
    assert payments.mailer.notes[0]["reason"] == "unexpected charge"
    assert watcher.ledger.due_followups() == []
    assert watcher.ledger.flags()


def test_hold_payment_only_accepts_known_job_id(tmp_path):
    watcher = watching(tmp_path)
    payments = Payments()
    tools = {"actions": [
        {"tool": "hold_payment", "text": "", "reason": "", "details": "", "urgency": "",
         "id": "AB12CD34", "minutes": 0, "message": ""},
        {"tool": "reply_to_user", "text": "I put the Austin Energy payment on hold.",
         "reason": "", "details": "", "urgency": "", "id": "", "minutes": 0, "message": ""},
    ]}
    out = guide(FakeLLM(tools=tools), "hold that bill", "question", watcher=watcher, payments=payments)
    assert payments.held == ["AB12CD34"]
    assert "Austin Energy" in out[0]


def test_no_finds_dentist_alert_in_demo_sqlite(tmp_path, monkeypatch):
    demo = tmp_path / "demo.sqlite"
    monkeypatch.setattr("backend.agent.guide.DEMO_LEDGER", demo)
    store = Ledger(demo)
    store.store_verdict(DUPLICATE, "There are two $120 Dentist charges today. Were you expecting both?")
    store.mark_delivered(DUPLICATE["dedupe_key"])
    store.close()
    empty = Watcher(Ledger(tmp_path / "empty.sqlite"),
                    lambda: {"source": "test", "balance_cents": 221300, "bills": [],
                             "deposits": [], "purchases": []})
    payments = Payments()
    out = Agent(FakeLLM(intent=intent("no")), FakeNessie(), "Margaret",
                payments=payments, watcher=empty).respond("No")
    assert "Dentist" in out[0] and "caregiver" in out[0].lower()
    assert payments.mailer.notes[0]["details"]["merchant"] == "Dentist"


def test_bare_yes_pays_the_open_bill_even_if_a_charge_alert_is_open(tmp_path):
    class BillYes:
        mailer = Mailer()
        seen = []

        def handle(self, body, intent, sid):
            self.seen.append((body, intent, sid))
            return ["Thank you. I've asked your caregiver to approve paying "
                    "$120.00 to Austin Energy on Wednesday, Oct 28."]

        def snapshot_jobs(self):
            return [{"id": "8C7E9D43", "payee": "Austin Energy", "amount": "$120.00",
                     "date": "2026-10-28", "status": "awaiting_user"}]

    payments = BillYes()
    out = Agent(FakeLLM(intent=intent("yes")), FakeNessie(), "Margaret",
                payments=payments, watcher=watching(tmp_path)).respond("YES", message_id="SM-yes")
    assert "Austin Energy" in out[0] and "caregiver" in out[0].lower()
    assert "leave those charges" not in out[0]
    assert payments.seen == [("YES", "yes", "SM-yes")]


def test_no_attaches_to_the_dentist_alert_not_an_empty_payment_queue(tmp_path):
    class EmptyQueue:
        mailer = Mailer()

        def handle(self, body, intent, sid):
            return None

        def snapshot_jobs(self):
            return []

    payments = EmptyQueue()
    out = Agent(FakeLLM(intent=intent("no")), FakeNessie(), "Margaret",
                payments=payments, watcher=watching(tmp_path)).respond("No", message_id="SM-no")
    assert "Dentist" in out[0] and "caregiver" in out[0].lower()
    assert "nothing waiting" not in out[0].lower()
    assert payments.mailer.notes[0]["details"]["merchant"] == "Dentist"


def test_unclear_after_the_dentist_alert_asks_again(tmp_path):
    out = Agent(FakeLLM(intent=intent("unclear")), FakeNessie(), "Margaret",
                watcher=watching(tmp_path)).respond("huh")
    assert "two $120 Dentist charges" in out[0]
    assert "Sorry, I didn't catch that" not in out[0]


def test_yes_no_without_watcher_stay_unavailable():
    for label in ("yes", "no", "undo", "forwarded_message"):
        out = Agent(FakeLLM(intent=intent(label)), FakeNessie(), "Margaret").respond("whatever")
        assert out == [reply.NOT_AVAILABLE]
