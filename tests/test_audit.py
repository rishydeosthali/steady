from datetime import datetime, timedelta, timezone

from backend.agent import reply
from backend.agent.audit import DECLINE, NOTHING, OFFER, AuditLog, asks_for_summary, format_recap
from backend.agent.guide import guide
from backend.agent.loop import Agent
from backend.app import create_app
from backend.demo.autopay import SUMMARY_OFFER, AutopayDemo
from tests.conftest import FakeLLM, FakeNessie
from tests.test_app import InlineExecutor, RecordingTwilio
from tests.test_guide import Payments, watching
from tests.test_loop import intent


def frozen(path, start=None, pause=30):
    now = {"t": start or datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)}

    def clock():
        return now["t"]

    log = AuditLog(path, pause_seconds=pause, clock=clock)
    return log, now


def test_rows_are_append_only_and_recap_uses_stored_reasons(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite")
    first = log.append("question", "You asked about balance.", needs=["balance"])
    log.append("blocked", "Paying is not turned on.")
    log.append("reply", "sent to Margaret", text="ignored in recap")
    rows = log.events()
    assert [row["seq"] for row in rows] == [1, 2, 3]
    assert first["reason"] == "You asked about balance."
    recap = log.recap()[0]
    assert "You asked about balance." in recap
    assert "Paying is not turned on" in recap
    assert "ignored in recap" not in recap
    log.close()


def test_idle_offer_then_yes_sends_recap(tmp_path):
    log, now = frozen(tmp_path / "audit.sqlite")
    log.append("question", "You asked about bills.")
    log.note_activity()
    assert log.offer_if_idle() is None
    now["t"] += timedelta(seconds=30)
    assert log.offer_if_idle() == OFFER
    assert log.offer_if_idle() is None
    recap = log.take_offer("yes")
    assert recap[0].startswith("Here's what happened")
    assert "You asked about bills." in recap[0]
    log.close()


def test_idle_offer_no_declines(tmp_path):
    log, now = frozen(tmp_path / "audit.sqlite")
    log.append("blocked", "Paying is not turned on.")
    log.note_activity()
    now["t"] += timedelta(seconds=30)
    assert log.offer_if_idle() == OFFER
    assert log.take_offer("no") == [DECLINE]
    log.close()


def test_named_payment_does_not_steal_the_recap(tmp_path):
    log, now = frozen(tmp_path / "audit.sqlite")
    log.append("decision", "payment waiting")
    log.note_activity()
    now["t"] += timedelta(seconds=30)
    log.offer_if_idle()
    assert log.take_offer("yes", named_payment=True) is None
    log.close()


def test_empty_log_has_no_offer_and_says_nothing_to_recap(tmp_path):
    log, now = frozen(tmp_path / "audit.sqlite")
    log.note_activity()
    now["t"] += timedelta(seconds=30)
    assert log.offer_if_idle() is None
    assert log.recap() == [NOTHING]
    log.close()


def test_agent_logs_intent_question_and_pay_block(tmp_path):
    audit = AuditLog(tmp_path / "audit.sqlite")
    llm = FakeLLM(intent=intent("question", ["balance"], restatement="checking balance"),
                  reply={"messages": ["You have $1,500.50 in checking."]})
    out = Agent(llm, FakeNessie(), "Margaret", audit=audit).respond("What is my balance?")
    assert out == ["You have $1,500.50 in checking."]
    kinds = [e["kind"] for e in audit.events()]
    assert kinds == ["intent", "question", "reply"]
    blocked = Agent(FakeLLM(intent=intent("pay_request")), FakeNessie(), "Margaret",
                    audit=audit).respond("pay the man")
    assert blocked == [reply.PAY_NOT_ON]
    assert any(e["kind"] == "blocked" for e in audit.events())
    audit.close()


def test_guide_logs_tools_that_ran_and_skips(tmp_path):
    audit = AuditLog(tmp_path / "audit.sqlite")
    payments = Payments()
    tools = {"actions": [
        {"tool": "hold_payment", "text": "", "reason": "unknown job", "details": "",
         "urgency": "", "id": "not-a-job", "minutes": 0, "message": ""},
        {"tool": "reply_to_user", "text": "I left the Austin Energy payment as it is.",
         "reason": "told her", "details": "", "urgency": "", "id": "", "minutes": 0, "message": ""},
    ]}
    out = guide(FakeLLM(tools=tools), "hold that bill", "question",
                watcher=watching(tmp_path), payments=payments, audit=audit)
    assert "Austin Energy" in out[0]
    tools_logged = [e for e in audit.events() if e["kind"] == "tool"]
    assert any(e["detail"]["tool"] == "hold_payment" and e["detail"]["ran"] is False
               for e in tools_logged)
    assert any(e["detail"]["tool"] == "reply_to_user" and e["detail"]["ran"] is True
               for e in tools_logged)
    audit.close()


def test_agent_yes_after_pause_returns_recap(tmp_path):
    log, now = frozen(tmp_path / "audit.sqlite", pause=5)
    agent = Agent(FakeLLM(intent=intent("question", ["bills"]), reply={"messages": []}),
                  FakeNessie(), "Margaret", audit=log)
    agent.respond("what bills do I have")
    now["t"] += timedelta(seconds=6)
    assert log.offer_if_idle() == OFFER
    out = Agent(FakeLLM(intent=intent("yes")), FakeNessie(), "Margaret", audit=log).respond("yes")
    assert out[0].startswith("Here's what happened")
    assert "You asked about bills." in out[0]
    log.close()


def test_audit_page_is_local_and_lists_events(settings, tmp_path):
    audit = AuditLog(tmp_path / "audit.sqlite")
    audit.append("decision", "Senior approved the exact read-back.")
    llm = FakeLLM(intent=intent("unclear"))
    twilio = RecordingTwilio(settings)
    app = create_app(settings, agent=Agent(llm, FakeNessie(), "Margaret", audit=audit),
                     twilio=twilio, executor=InlineExecutor())
    client = app.test_client()
    page = client.get("/audit")
    assert page.status_code == 200 and b"Decision log" in page.data
    assert client.get("/audit/static/log.css").status_code == 200
    events = client.get("/audit/events").json["events"]
    assert events[-1]["reason"] == "Senior approved the exact read-back."
    audit.close()


def test_audit_idle_route_offers_once(settings, tmp_path):
    now = {"t": datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)}
    audit = AuditLog(tmp_path / "audit.sqlite", pause_seconds=5, clock=lambda: now["t"])
    audit.append("blocked", "Paying is not turned on.")
    audit.note_activity()
    now["t"] += timedelta(seconds=6)
    llm = FakeLLM(intent=intent("unclear"))
    twilio = RecordingTwilio(settings)
    twilio.record_inbound(settings.mom_whatsapp_to)
    app = create_app(settings, agent=Agent(llm, FakeNessie(), "Margaret", audit=audit),
                     twilio=twilio, executor=InlineExecutor())
    client = app.test_client()
    first = client.post("/audit/idle")
    assert first.json["offered"] is True and first.json["text"] == OFFER
    assert twilio.sent[-1][1] == OFFER
    assert client.post("/audit/idle").json["offered"] is False
    audit.close()


def test_recap_groups_repeats_and_skips_internal_notes():
    text = format_recap([
        {"kind": "decision", "reason": "proposal from external invoice",
         "detail": {"event": "proposal_from_external_invoice", "payee": "Austin Energy", "amount": "$120.00"}},
        {"kind": "decision", "reason": "proposal from external invoice",
         "detail": {"event": "proposal_from_external_invoice", "payee": "Water Co", "amount": "$45.25"}},
        {"kind": "decision", "reason": "proposal from external invoice",
         "detail": {"event": "proposal_from_external_invoice", "payee": "Spectrum", "amount": "$89.99"}},
        {"kind": "decision", "reason": "Read a bill photo. Waiting for YES to save it.",
         "detail": {"payee": "Austin Energy", "amount": "$118.60"}},
        {"kind": "decision", "reason": "read a bill photo", "detail": {}},
        {"kind": "receipt", "reason": "Saved the bill from the photo in Nessie.",
         "detail": {"payee": "Austin Energy", "amount": "$118.60"}},
        {"kind": "decision", "reason": "bill photo reply handled in code", "detail": {}},
    ])[0]
    assert text.startswith("Here's what happened")
    assert "Austin Energy, Water Co, and Spectrum" in text
    assert "You sent a photo. After you said yes, I saved $118.60 to Austin Energy." in text
    assert "proposal from external invoice" not in text
    assert "bill photo reply handled in code" not in text
    assert "Waiting for YES" not in text
    assert "\n\n" in text


def test_asks_for_summary_is_code_not_the_model():
    assert asks_for_summary("Can I have a summary?")
    assert asks_for_summary("what happened")
    assert asks_for_summary("show me the log history")
    assert asks_for_summary("what's our log")
    assert not asks_for_summary("what bills do I have")
    assert not asks_for_summary("how much money do we have")
    assert not asks_for_summary("yes")


def test_agent_gives_a_recap_when_asked(tmp_path):
    audit = AuditLog(tmp_path / "audit.sqlite")
    Agent(FakeLLM(intent=intent("question", ["bills"]), reply={"messages": []}),
          FakeNessie(), "Margaret", audit=audit).respond("what bills do I have")
    out = Agent(FakeLLM(intent=intent("unclear")), FakeNessie(), "Margaret",
                audit=audit).respond("give me a summary")
    assert out[0].startswith("Here's what happened")
    assert "You asked about bills." in out[0]
    audit.close()


def test_autopay_gives_a_recap_when_asked_even_if_a_bill_is_waiting(tmp_path):
    runtime = AutopayDemo(tmp_path / "demo.sqlite3")
    runtime.tick()
    assert runtime.state()["requests"]
    runtime.receive("senior", "Can I have a summary?", "ask-summary")
    recap = [m["body"] for m in runtime.state()["messages"] if m["body"].startswith("Here's what happened")]
    assert recap and "Bill due in" in recap[-1]


def test_autopay_offers_summary_after_a_pause_then_recaps(tmp_path):
    now = {"t": datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)}
    runtime = AutopayDemo(tmp_path / "demo.sqlite3", clock=lambda: now["t"], pause_seconds=5)
    runtime.tick()
    req = next(iter(runtime.state()["requests"].values()))
    runtime.receive("senior", "Yes", "yes-1", request_id=req["id"])
    runtime.advance(2)
    assert runtime.state()["requests"][req["id"]]["status"] == "paid"
    now["t"] += timedelta(seconds=6)
    runtime.tick()
    assert any(m["body"] == SUMMARY_OFFER for m in runtime.state()["messages"])
    runtime.receive("senior", "Yes", "yes-summary")
    recap = [m["body"] for m in runtime.state()["messages"] if m["body"].startswith("Here's what happened")]
    assert recap and "Simulated payment recorded once" in recap[-1]
