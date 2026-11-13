"""Hosted operator actions share state with the signed WhatsApp agent."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from flask import Flask

from backend.agent.facts import gather_facts
from backend.simulation.runtime import Conflict, SimulationRuntime
from backend.simulation.web import register_dashboard
from tests.conftest import MOM, FakeLLM
from tests.test_app import InlineExecutor, RecordingTwilio, form, sign
from tests.test_external_biller import FakeMailer

@pytest.fixture
def runtime(tmp_path, settings):
    transport = RecordingTwilio(settings)
    transport.record_inbound(MOM)
    result = SimulationRuntime(settings, transport, tmp_path, mailer=FakeMailer(), today="2026-11-11")
    yield result
    result.stop()


def command(runtime, operation, body=None, key=None):
    runtime.command(operation, {"run_id":runtime.store.state()["run_id"], **(body or {})}, key or uuid4().hex)


def first_job(runtime):
    return next(iter(runtime.payments.state()["jobs"].values()))


def test_public_dashboard_never_exposes_approval_tokens(runtime):
    app = Flask(__name__)
    register_dashboard(app, runtime)
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert b"access-key" not in client.get("/").data
    assert client.get("/simulation/api/state").status_code == 200
    runtime.tick()
    job = first_job(runtime)
    runtime.payments.handle("YES "+job["id"], "yes", "SMstudio-consent")
    raw = runtime.payments.state()
    pending_token = next(m["token"] for m in raw["outbox"] if m["kind"] == "caregiver_email")
    response = client.get("/simulation/api/state", headers={"X-Forwarded-For":"203.0.113.4"})
    assert response.status_code == 200
    assert pending_token not in response.text
    assert "caregiver_token_hash" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    response = client.post("/simulation/api/expense", json={
        "run_id":runtime.store.state()["run_id"], "merchant":"Demo expense", "amount":"20"},
        headers={"Idempotency-Key":uuid4().hex})
    assert response.status_code == 200


def test_expense_updates_shared_facts_once_even_with_concurrent_retry(runtime):
    body = {"run_id":runtime.store.state()["run_id"], "merchant":"Car repair", "amount":"400.01"}
    key = uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:runtime.command("expense",body,key), range(2)))
    assert runtime.state()["balance_cents"] == 181299
    account = runtime.bank.resolve("Margaret")
    facts = gather_facts(runtime.bank, account["_id"], ("balance",))
    assert runtime.payments.with_available_balance(account["_id"], facts)["balance"]["amount"] == "$1,812.99"
    assert len([p for p in runtime.bank.get_purchases(account["_id"]) if p["description"] == "Car repair"]) == 1
    with pytest.raises(Conflict):
        runtime.command("expense", {**body,"amount":"50"}, key)


@pytest.mark.parametrize("amount",["0","-1","NaN","Infinity","1.001",True,"999999999"])
def test_bad_money_never_changes_cash(runtime, amount):
    with pytest.raises(ValueError):
        command(runtime,"expense",{"merchant":"Test","amount":amount})
    assert runtime.store.state()["balance_cents"] == 221300


def test_posted_expense_is_not_subtracted_twice_by_watcher(runtime):
    command(runtime,"expense",{"merchant":"Repair","amount":"1800"})
    assert runtime.state()["balance_cents"] == 41300
    assert not [e for e in runtime.state()["alerts"] if e["kind"] == "pocket_shortfall"]
    command(runtime,"expense",{"merchant":"Pharmacy","amount":"220"})
    short = next(e for e in runtime.state()["alerts"] if e["kind"] == "pocket_shortfall")
    assert short["facts"]["left_after"] == "$193.00"


def test_duplicate_alert_reaches_phone_and_future_questions_see_same_expense(runtime):
    command(runtime,"expense",{"merchant":"Dentist","amount":"120"})
    alerts = runtime.watcher.ledger.events("mom")
    assert any(e["kind"] == "duplicate" and e["delivered"] for e in alerts)
    assert runtime.state()["balance_cents"] == 209300
    assert any("Dentist" in text or "dentist" in text for _,text in runtime.twilio.sent)


def test_closed_session_queues_alert_then_delivers_on_open(runtime):
    opened = [False]
    runtime.twilio.session_open = lambda _:opened[0]
    command(runtime,"expense",{"merchant":"Dentist","amount":"120"})
    assert not runtime.twilio.sent
    assert any(e["kind"] == "duplicate" and not e["delivered"] for e in runtime.watcher.ledger.events("mom"))
    opened[0] = True
    runtime.tick()
    assert any(e["kind"] == "duplicate" and e["delivered"] for e in runtime.watcher.ledger.events("mom"))
    count = len(runtime.twilio.sent)
    runtime.tick()
    assert len(runtime.twilio.sent) == count


def test_new_expense_holds_approved_future_payment_and_old_caregiver_link(runtime):
    command(runtime,"expense",{"merchant":"Opening expense","amount":"2000"})
    # $213 settled cash: the first safe payment date is after income in two days.
    runtime.tick()
    job = first_job(runtime)
    assert job["scheduled_for"] == "2026-11-13"
    runtime.payments.handle("YES "+job["id"],"yes","SMfuture-consent")
    runtime.payments.flush()
    token = runtime.payments.mailer.token
    runtime.payments.caregiver_decide(token,"approve")
    pending = next(d for d in runtime.store.state()["deposits"] if d["status"] == "pending")
    command(runtime,"delay-income",{"deposit_id":pending["_id"],"days":7})
    assert first_job(runtime)["status"] == "held"
    assert not runtime.store.state()["receipts"]
    runtime.payments.caregiver_decide(token,"approve")
    assert first_job(runtime)["status"] == "held"


def test_changed_bill_needs_new_proposal_and_two_new_approvals(runtime):
    runtime.tick()
    old = first_job(runtime)
    runtime.payments.handle("YES "+old["id"],"yes","SMold-consent")
    runtime.payments.flush()
    token = runtime.payments.mailer.token
    command(runtime,"bill",{"payee":"Austin Energy","amount":"180","due_date":"2026-11-14"})
    jobs = runtime.payments.state()["jobs"]
    assert jobs[old["id"]]["status"] == "held"
    newer = next(j for j in jobs.values() if j["id"] != old["id"] and j["payee"] == "Austin Energy")
    assert newer["status"] == "awaiting_user" and not newer["user_approved"]
    runtime.payments.caregiver_decide(token,"approve")
    runtime.tick()
    assert not runtime.store.state()["receipts"]


def test_income_settles_once_and_paid_bill_has_one_receipt_after_restart(runtime, settings):
    runtime.tick()
    job = first_job(runtime)
    runtime.payments.handle("YES "+job["id"],"yes","SMpay-consent")
    runtime.payments.flush()
    runtime.payments.caregiver_decide(runtime.payments.mailer.token,"approve")
    runtime.tick()
    assert len(runtime.store.state()["receipts"]) == 1
    assert runtime.state()["balance_cents"] == 209300
    restarted = SimulationRuntime(settings,runtime.twilio,runtime.directory,mailer=FakeMailer())
    restarted.tick()
    assert len(restarted.store.state()["receipts"]) == 1
    pending = next(d for d in restarted.store.state()["deposits"] if d["status"] == "pending")
    key = uuid4().hex
    command(restarted,"settle-income",{"deposit_id":pending["_id"]},key)
    balance = restarted.state()["balance_cents"]
    command(restarted,"settle-income",{"deposit_id":pending["_id"]},key)
    assert restarted.state()["balance_cents"] == balance == 396700


def test_reset_rotates_identity_clears_consent_and_rejects_stale_browser(runtime):
    runtime.tick()
    old_run = runtime.store.state()["run_id"]
    old_job = first_job(runtime)
    runtime.payments.handle("YES "+old_job["id"],"yes","SMreset-consent")
    runtime.payments.flush()
    token = runtime.payments.mailer.token
    runtime.save("Before surprise")
    runtime.reset()
    assert runtime.store.state()["run_id"] != old_run
    assert not runtime.payments.state()["jobs"]
    assert runtime.payments.caregiver_view(token) is None
    with pytest.raises(Conflict):
        runtime.command("expense",{"run_id":old_run,"merchant":"Old tab","amount":"10"},uuid4().hex)
    assert runtime.saved()[0]["name"] == "Before surprise"
    runtime.reset(saved_id=runtime.saved()[0]["id"])
    assert not runtime.payments.state()["jobs"]


def test_clock_settles_expected_income_without_double_credit(runtime):
    command(runtime,"advance")
    assert runtime.state()["balance_cents"] == 221300
    command(runtime,"advance")
    assert runtime.state()["balance_cents"] == 408700
    command(runtime,"check")
    assert runtime.state()["balance_cents"] == 408700


def test_recovered_funds_automatically_get_fresh_consent(runtime):
    runtime.tick()
    old = first_job(runtime)
    runtime.payments.handle("YES "+old["id"],"yes","SMapproved-before-expense")
    runtime.payments.flush()
    token = runtime.payments.mailer.token
    runtime.payments.caregiver_decide(token,"approve")
    command(runtime,"expense",{"merchant":"Emergency repair","amount":"2000"})
    jobs = runtime.payments.state()["jobs"]
    assert jobs[old["id"]]["status"] == "superseded"
    fresh = list(jobs.values())[-1]
    assert fresh["scheduled_for"] == "2026-11-13"
    assert fresh["status"] == "awaiting_user"
    assert not fresh["user_approved"] and fresh["caregiver_approved"] is None
    assert fresh["idempotency_key"] != old["idempotency_key"]
    runtime.payments.caregiver_decide(token,"approve")
    assert not runtime.store.state()["receipts"]


def test_replan_does_not_revive_a_senior_hold(runtime):
    runtime.tick()
    old = first_job(runtime)
    runtime.payments.hold_job(old["id"])
    command(runtime,"replan")
    assert first_job(runtime)["status"] == "held"
    assert len(runtime.payments.state()["jobs"]) == 1


def test_saving_request_is_idempotent(runtime):
    runtime.save("Repeatable", "same-save-request")
    runtime.save("Repeatable", "same-save-request")
    assert len(runtime.saved()) == 1


def test_failed_followup_is_pending_until_the_send_succeeds(runtime):
    runtime.watcher.ledger.schedule_followup(1,"Checking back")
    ledger = runtime.watcher.ledger
    with ledger._lock, ledger._db:
        ledger._db.execute("UPDATE followups SET fire_at='2000-01-01T00:00:00+00:00'")
    original = runtime.twilio.send_reply
    runtime.twilio.send_reply = lambda *_: (_ for _ in ()).throw(RuntimeError("temporary failure"))
    runtime.flush_alerts()
    with ledger._lock:
        assert ledger._db.execute("SELECT sent FROM followups").fetchone()[0] == 0
    runtime.twilio.send_reply = original
    runtime.flush_alerts()
    with ledger._lock:
        assert ledger._db.execute("SELECT sent FROM followups").fetchone()[0] == 1


def test_original_signed_webhook_uses_dashboard_account(monkeypatch,tmp_path,settings):
    from backend.app import create_app
    llm = FakeLLM(intent={"intent":"question","confidence":.99,"needs":["balance"],"restatement":"balance"},reply={"messages":[]})
    monkeypatch.setenv("STEADY_SIMULATION","1")
    monkeypatch.setenv("STEADY_EXTERNAL_BILLER","0")
    monkeypatch.setenv("STEADY_AUTONOMY","0")
    monkeypatch.setenv("STEADY_SIMULATION_DIR",str(tmp_path))
    monkeypatch.delenv("STEADY_DASHBOARD_TOKEN", raising=False)
    monkeypatch.setattr("backend.app.LLM",lambda *_:llm)
    monkeypatch.setattr("backend.payments.integration.build_mailer",lambda _:FakeMailer())
    twilio = RecordingTwilio(settings)
    app = create_app(settings,twilio=twilio,executor=InlineExecutor())
    runtime = app.extensions["simulation"]
    command(runtime,"expense",{"merchant":"Car repair","amount":"400"})
    data = form("What is my balance?",sid="SMstudio-balance")
    client = app.test_client()
    assert client.post("/whatsapp/inbound",data=data).status_code == 403
    response = client.post("/whatsapp/inbound",data=data,headers={"X-Twilio-Signature":sign(data)})
    assert response.status_code == 200
    assert any("$1,813.00" in text for _,text in twilio.sent)
    assert runtime.bank is not None


def test_hosted_entrypoint_starts_and_stops_the_monitor(monkeypatch, settings):
    import sys
    from types import SimpleNamespace
    from backend.hosted import main
    calls = []
    runtime = SimpleNamespace(twilio=object(), audit=object(), lock=threading.RLock(),
        start=lambda:calls.append("monitor started"), stop=lambda:calls.append("monitor stopped"))
    class Idle:
        def __init__(self,*args):
            pass
        def start(self):
            calls.append("idle started")
        def stop(self):
            calls.append("idle stopped")
    def serve(app, **options):
        assert options["host"] == "0.0.0.0" and options["port"] == 10000
        assert "monitor started" in calls
    monkeypatch.setenv("STEADY_SIMULATION","1")
    monkeypatch.setenv("PORT","10000")
    monkeypatch.setattr("backend.config.load_settings",lambda:settings)
    monkeypatch.setattr("backend.app.create_app",lambda _:SimpleNamespace(extensions={"simulation":runtime}))
    monkeypatch.setattr("backend.agent.audit.IdleWorker",Idle)
    monkeypatch.setitem(sys.modules,"waitress",SimpleNamespace(serve=serve))
    main()
    assert calls == ["monitor started","idle started","monitor stopped","idle stopped"]
