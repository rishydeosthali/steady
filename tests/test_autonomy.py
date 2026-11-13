"""Goal pursuit, actual tool observations, restart recovery, and authority boundaries."""

import json
from dataclasses import replace
from datetime import date

import pytest

from backend.agent.llm import LLMError
from backend.agent.loop import Agent
from backend.app import create_app
from backend.autonomy.budget import forecast
from backend.autonomy.controller import Controller
from backend.autonomy.memory import Memory
from backend.payments.service import PaymentService
from tests.conftest import MOM, FakeLLM, FakeNessie
from tests.test_app import InlineExecutor, RecordingTwilio, form, sign
from tests.test_external_biller import FakeMailer, provider, setup_service, job, approve


class Reasoner:
    def __init__(self, actions=(), goal=None):
        self.actions = list(actions)
        self.goal = goal or dict(target="$300", deadline="2026-12-25", buffer="$200", weekly_essentials="$100")
        self.contexts = []

    def json_completion(self, system, user, schema_name, schema):
        if schema_name == "steady_goal":
            return self.goal
        self.contexts.append(json.loads(user))
        if not self.actions:
            raise LLMError("No action scripted")
        return self.actions.pop(0)


def action(tool, key="", minutes=10):
    return dict(tool=tool, id=key, reason="React to the observed facts.", minutes=minutes)


def make(tmp_path, settings, llm=None, bank=None, payments=None):
    memory = Memory(tmp_path / "goals.sqlite")
    twilio = RecordingTwilio(settings)
    twilio.record_inbound(MOM)
    controller = Controller(memory, llm or Reasoner(), bank or FakeNessie(balance=2000, bills=[]),
                            twilio, MOM, payments=payments, clock=lambda: date(2026, 11, 10))
    return controller


def activate(controller, weekly=10000, buffer=20000, deadline="2026-12-25"):
    goal = dict(id="ABCD1234", kind="budget", status="active", target_cents=30000,
                deadline=deadline, buffer_cents=buffer, weekly_essentials_cents=weekly,
                priority="essential bills first")
    with controller.memory.transaction() as state:
        state["goals"][goal["id"]] = goal
    return goal


def test_goal_requires_exact_confirmation_and_survives_restart(tmp_path, settings):
    controller = make(tmp_path, settings)
    text = "Save $300 by 2026-12-25, keep $200 and allow $100 weekly for essentials"
    reply = controller.handle(text, "draft")
    state = controller.memory.read()
    assert not state["goals"] and "CONFIRM GOAL" in reply[0]
    ref = next(iter(state["drafts"]))
    assert controller.handle("yes", "bare") is None
    assert not controller.memory.read()["goals"]
    controller.handle("CONFIRM GOAL " + ref, "confirm")
    restarted = make(tmp_path, settings)
    assert restarted.memory.read()["goals"][ref]["target_cents"] == 30000
    assert restarted.handle("CONFIRM GOAL " + ref, "confirm") == []


def test_goal_rejects_model_invented_amounts_and_low_buffer(tmp_path, settings):
    llm = Reasoner(goal=dict(target="$999", deadline="2026-12-25", buffer="$200", weekly_essentials="$100"))
    controller = make(tmp_path, settings, llm)
    controller.handle("Save $300 by 2026-12-25 with $200 and $100 weekly", "bad")
    assert not controller.memory.read()["drafts"]
    llm.goal.update(target="$300", buffer="$10")
    controller.handle("Save $300 by 2026-12-25 with $10 and $100 weekly", "low")
    assert not controller.memory.read()["drafts"]


def test_new_goal_does_not_replace_active_goal_without_confirmation(tmp_path, settings):
    controller = make(tmp_path, settings)
    original = activate(controller)
    controller.handle("Save $300 by 2026-12-25 with $200 and $100 weekly", "draft")
    assert controller.memory.read()["drafts"]
    assert controller.memory.read()["goals"][original["id"]]["status"] == "active"


def test_budget_conserves_cents_and_does_not_double_count_settled_income(tmp_path, settings):
    bank = FakeNessie(balance=1000, bills=[], deposits=[
        dict(amount=500, status="completed", transaction_date="2026-11-10"),
        dict(amount=200, status="pending", transaction_date="2026-11-11")])
    controller = make(tmp_path, settings, bank=bank)
    goal = activate(controller, weekly=7000, deadline="2026-11-11")
    report = forecast(goal, controller.observe())
    assert report["projected_capacity_cents"] == 98000  # 1000+200-20-200
    assert report["conditional_on_pending_income"]
    assert report["verified_saved_cents"] is None
    bank.deposits[1]["transaction_date"] = "2026-11-10"
    assert forecast(goal, controller.observe())["projected_capacity_cents"] == 78000


def test_missing_essentials_or_bill_date_prevents_confident_budget(tmp_path, settings):
    controller = make(tmp_path, settings, bank=FakeNessie(bills=[dict(payee="Unknown", payment_amount=50)]))
    goal = activate(controller, weekly=None)
    assert forecast(goal, controller.observe())["status"] == "needs_essentials"
    goal["weekly_essentials_cents"] = 0
    assert forecast(goal, controller.observe())["status"] == "incomplete"


def test_model_observes_result_and_replans_when_balance_changes(tmp_path, settings):
    llm = Reasoner([action("budget", "ABCD1234"), action("report_budget", "ABCD1234"), action("wait")])
    controller = make(tmp_path, settings, llm)
    goal = activate(controller, weekly=0)
    controller.run()
    assert llm.contexts[1]["results"][0]["result"]["status"] == "on_track"
    controller.flush()
    assert len(controller.twilio.sent) == 1
    controller.run()
    assert len(llm.contexts) == 3  # unchanged facts do not spend more model calls
    controller.nessie.balance = 250
    llm.actions = [action("budget", goal["id"]), action("report_budget", goal["id"]), action("wait")]
    controller.run()
    assert controller.memory.read()["reports"][goal["id"]]["status"] == "at_risk"
    assert controller.memory.read()["goals"][goal["id"]]["target_cents"] == 30000
    controller.flush()
    assert "gap" in controller.twilio.sent[-1][1]


def test_restart_resumes_due_work_with_saved_goal_and_prior_observations(tmp_path, settings):
    controller = make(tmp_path, settings, Reasoner([action("wait")]))
    activate(controller)
    controller.run()
    llm = Reasoner([action("budget", "ABCD1234"), action("wait")])
    restarted = make(tmp_path, settings, llm)
    with restarted.memory.transaction() as state:
        state["next_check"] = 0
    restarted.run()
    assert llm.contexts[0]["goals"][0]["id"] == "ABCD1234"
    assert llm.contexts[0]["recent_decisions"]
    assert len(restarted.memory.read()["snapshots"]) == 2


def test_model_cannot_change_goal_or_execute_payment_and_stops_repeating(tmp_path, settings):
    llm = Reasoner([action("execute_payment")])
    controller = make(tmp_path, settings, llm)
    activate(controller)
    controller.run()
    assert controller.memory.read()["events"][-1]["kind"] == "run_unavailable"
    assert not controller.nessie.writes
    llm.actions = [action("budget", "ABCD1234"), action("budget", "ABCD1234")]
    controller.run(force=True)
    assert controller.memory.read()["events"][-1]["kind"] == "no_progress"


def test_closed_session_and_transient_send_failure_preserve_notifications(tmp_path, settings):
    controller = make(tmp_path, settings)
    controller.memory.queue("test", "A confirmed fact")
    controller.twilio._last_inbound.clear()
    controller.flush()
    assert controller.memory.read()["outbox"][0]["status"] == "pending"
    controller.twilio.record_inbound(MOM)
    original = controller.twilio.send_reply
    controller.twilio.send_reply = lambda *args: (_ for _ in ()).throw(RuntimeError("temporary"))
    controller.flush()
    assert controller.memory.read()["outbox"][0]["status"] == "pending"
    controller.twilio.send_reply = original
    controller.flush()
    controller.flush()
    assert len(controller.twilio.sent) == 1


def test_pause_keeps_protected_cushion_and_requires_explicit_resume(tmp_path, settings):
    controller = make(tmp_path, settings)
    activate(controller, buffer=40000)
    controller.handle("PAUSE GOAL ABCD1234", "pause")
    assert controller.memory.protected_buffer() == 40000
    controller.run()
    assert not controller.llm.contexts
    controller.handle("RESUME GOAL ABCD1234", "resume")
    assert controller.memory.read()["goals"]["ABCD1234"]["status"] == "active"


def test_database_lease_prevents_two_controllers_running_together(tmp_path, settings):
    first = make(tmp_path, settings)
    second = make(tmp_path, settings)
    activate(first)
    with first.memory.lease() as acquired:
        assert acquired
        second.run(force=True)
        assert not second.llm.contexts


def test_expired_deadline_requests_review_instead_of_claiming_success(tmp_path, settings):
    controller = make(tmp_path, settings)
    activate(controller, deadline="2026-11-09")
    controller.run()
    state = controller.memory.read()
    assert state["goals"]["ABCD1234"]["status"] == "needs_review"
    assert "deadline has passed" in state["outbox"][0]["text"]


def test_autonomy_routes_and_sender_checks_are_private(tmp_path, settings):
    controller = make(tmp_path, settings)
    llm = FakeLLM()
    app = create_app(replace(settings, allow_any_sender=True), agent=Agent(llm, FakeNessie(), "Margaret"),
                     twilio=controller.twilio, autonomy=controller, executor=InlineExecutor())
    client = app.test_client()
    assert client.get("/goals", environ_base={"REMOTE_ADDR": "203.0.113.1"}).status_code == 403
    assert client.get("/goals", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 200
    assert client.get("/goals", environ_base={"REMOTE_ADDR": "127.0.0.1"},
                      headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 403
    data = form("budget", from_="whatsapp:+15559999999")
    client.post("/whatsapp/inbound", data=data, headers={"X-Twilio-Signature": sign(data)})
    assert not controller.twilio.sent


def test_funds_recovery_creates_new_proposal_with_no_inherited_consent(provider, tmp_path, settings):
    biller, _ = provider
    bank = FakeNessie(balance=300, bills=[], deposits=[
        dict(status="pending", amount=1000, transaction_date="2026-10-26")])
    service, bank, _ = setup_service(tmp_path, settings, biller, bank)
    approve(service)
    original = job(service)
    service.advance(1)  # income failed to settle: execution must hold
    assert job(service)["hold_reason"] == "funds"
    bank.balance = 1300
    result = service.reconsider(original["invoice_id"])
    assert result["status"] == "awaiting_user"
    fresh = service.state()["jobs"][result["reference"]]
    assert fresh["idempotency_key"] != original["idempotency_key"]
    assert not fresh["user_approved"] and fresh["caregiver_approved"] is None
    assert not fresh["readback_delivered"]
    service.tick()
    assert biller.invoice(original["invoice_id"])["status"] == "unpaid"


@pytest.mark.parametrize("status", ["cancelled", "denied", "reconciling", "paid", "expired"])
def test_resolver_never_revives_prohibited_states(provider, tmp_path, settings, status):
    biller, _ = provider
    service, _, _ = setup_service(tmp_path, settings, biller)
    original = job(service)
    with service._tx() as (_, state):
        state["jobs"][original["id"]].update(status=status, hold_reason="funds")
    assert service.reconsider(original["invoice_id"])["status"] == "waiting"
    assert len(service.state()["jobs"]) == 1


def test_confirmed_budget_cushion_and_essentials_block_unsafe_payment(provider, tmp_path, settings):
    biller, _ = provider
    service, bank, _ = setup_service(tmp_path, settings, biller)
    approve(service)
    controller = make(tmp_path, settings, bank=bank, payments=service)
    activate(controller, weekly=70000, buffer=50000)
    bank.balance = 1000
    service.tick()
    assert job(service)["status"] == "held"
    assert biller.invoice(job(service)["invoice_id"])["status"] == "unpaid"


def test_claiming_readback_is_not_delivery_and_failed_sends_retry(provider, tmp_path, settings):
    biller, _ = provider
    twilio = RecordingTwilio(settings)
    twilio.record_inbound(MOM)
    service = PaymentService(tmp_path / "delivery.sqlite", biller, FakeNessie(balance=2000, bills=[]),
                             twilio, MOM, mailer=FakeMailer(), demo_date=date(2026, 10, 25))
    service.tick()
    prompt = service.claim_readback()
    assert prompt and not job(service)["readback_delivered"]
    original = twilio.send_reply
    twilio.send_reply = lambda *args: (_ for _ in ()).throw(RuntimeError("temporary"))
    service.flush()
    assert not job(service)["readback_delivered"]
    twilio.send_reply = original
    service.flush()
    assert job(service)["readback_delivered"] and len(twilio.sent) == 1


def test_agent_reasons_over_funds_recovery_and_tracks_verified_receipt(provider, tmp_path, settings):
    biller, _ = provider
    bank = FakeNessie(balance=210, bills=[])
    service, bank, _ = setup_service(tmp_path, settings, biller, bank)
    original = job(service)
    assert original["status"] == "held"
    invoice_id = original["invoice_id"]
    llm = Reasoner([action("inspect_bill", invoice_id), action("resolve_bill", invoice_id),
                    action("report_bill", invoice_id), action("wait")])
    controller = make(tmp_path, settings, llm=llm, bank=bank, payments=service)
    bank.balance = 2000
    controller.run()
    assert llm.contexts[2]["results"][1]["result"]["status"] == "awaiting_user"
    fresh = list(service.state()["jobs"].values())[-1]
    assert fresh["invoice_id"] == invoice_id
    assert not fresh["user_approved"]
    assert controller.memory.read()["goals"]["bill:" + invoice_id]["status"] == "active"
    controller.flush()
    assert "Status: awaiting_user" in controller.twilio.sent[-1][1]
    service.flush()
    service.handle("YES " + fresh["id"], "yes", "fresh-approval")
    service.flush()
    service.caregiver_decide(service.mailer.token, "approve")
    service.tick()
    controller.observe()
    goal = controller.memory.read()["goals"]["bill:" + invoice_id]
    assert goal["status"] == "complete"
    assert goal["receipt"]["id"] == biller.receipt(fresh["idempotency_key"])["id"]


def test_changed_queued_budget_supersedes_stale_plan_before_delivery(tmp_path, settings):
    controller = make(tmp_path, settings)
    controller.memory.queue("budget:ABCD1234:day:old", "Old plan")
    controller.memory.queue("budget:ABCD1234:day:new", "Updated plan")
    controller.flush()
    assert controller.twilio.sent == [(MOM, "Updated plan")]


def test_failed_observation_does_not_use_stale_money_or_finish_goal(tmp_path, settings):
    controller = make(tmp_path, settings)
    activate(controller)
    controller.observe()
    from backend.nessie.client import NessieError
    controller.nessie.get_balance = lambda *args: (_ for _ in ()).throw(NessieError("Unavailable"))
    controller.run(force=True)
    assert not controller.llm.contexts
    assert not controller.memory.read()["reports"]
    assert controller.memory.read()["goals"]["ABCD1234"]["status"] == "active"


def test_negative_balance_reports_a_shortfall_instead_of_assuming_zero(tmp_path, settings):
    controller = make(tmp_path, settings, bank=FakeNessie(balance=-50, bills=[]))
    goal = activate(controller, weekly=0)
    report = forecast(goal, controller.observe())
    assert report["status"] == "at_risk" and report["buffer_shortfall_cents"] == 25000


def test_followup_is_pending_until_send_is_acknowledged(tmp_path):
    from backend.watch.ledger import Ledger
    from datetime import datetime, timedelta, timezone
    ledger = Ledger(tmp_path / "watch.sqlite")
    ledger.schedule_followup(1, "Checking back")
    clock = datetime.now(timezone.utc) + timedelta(minutes=2)
    pending = ledger.pending_followups(clock)
    assert pending == ledger.pending_followups(clock)
    ledger.acknowledge_followup(pending[0]["seq"])
    assert ledger.pending_followups(clock) == []
    ledger.close()


def test_model_outage_respects_saved_retry_time_for_unchanged_facts(tmp_path, settings):
    llm = Reasoner([])
    controller = make(tmp_path, settings, llm)
    activate(controller)
    controller.run()
    assert len(llm.contexts) == 1
    controller.run()
    assert len(llm.contexts) == 1
