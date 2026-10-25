"""Real HTTP to a separate provider process; fake Twilio/LLM/Nessie only.

These tests prove the invoice changes in the provider's own database, not just
inside Steady's state. They need no account credentials or public network.
"""

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
from datetime import date
from uuid import uuid4

import pytest
import requests

from backend.agent.loop import Agent
from backend.app import create_app
from backend.payments.client import BillerClient, BillerError
from backend.payments.integration import register_controls
from backend.payments.service import PaymentService
from tests.conftest import MOM, FakeLLM, FakeNessie
from tests.test_app import InlineExecutor, RecordingTwilio, form, sign

BILLER_TOKEN = "synthetic-provider-test-token"
CONTROL_TOKEN = "synthetic-controller-test-token"
ANCHOR = date(2026,10,25)


@pytest.fixture
def provider(tmp_path):
    env = dict(os.environ,BILLER_API_TOKEN=BILLER_TOKEN)
    process = subprocess.Popen([sys.executable,"-m","biller_simulator","--port","0",
                                "--db",str(tmp_path/"provider.sqlite3"),"--as-of",ANCHOR.isoformat()],
                               env=env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    try:
        # select() only accepts sockets on Windows, so read the ready line on a thread.
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(process.stdout.readline()),daemon=True).start()
        try:
            ready = json.loads(lines.get(timeout=10))
        except queue.Empty:
            pytest.fail("Independent biller did not start")
        assert ready["simulation"] is True
        yield BillerClient(ready["url"],BILLER_TOKEN),process
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def payload(invoice):
    return {"invoice_id":invoice["id"],"customer_ref":invoice["customer_ref"],
            "amount_cents":invoice["amount_cents"],"currency":"USD", "invoice_version":invoice["version"],
            "due_date":invoice["due_date"],"simulation_date":ANCHOR.isoformat()}


class FakeMailer:
    """Records caregiver approval requests instead of sending email."""

    def __init__(self):
        self.sent = []
        self.fail = False

    def request_approval(self,payment,token):
        if self.fail:
            from backend.payments.caregiver import MailError
            raise MailError("Caregiver email failed: SMTPServerDisconnected")
        self.sent.append((payment,token))

    def notify(self,reason,details,urgency="soon"):
        self.sent.append(({"reason":reason,"details":details,"urgency":urgency},None))

    @property
    def token(self):
        return self.sent[-1][1]


def setup_service(tmp_path,settings,biller,bank=None,mailer=None):
    bank = bank or FakeNessie(balance=2213,bills=[])
    transport = RecordingTwilio(settings)
    transport.record_inbound(MOM)
    service = PaymentService(tmp_path/"steady.sqlite3",biller,bank,transport,MOM,demo_date=ANCHOR,
                             mailer=mailer or FakeMailer(),caregiver_name="Sarah")
    service.tick()
    service.flush()
    return service,bank,transport


def job(service):
    return next(iter(service.state()["jobs"].values()))


def senior_yes(service,sid="approval-sid"):
    service.handle("YES "+job(service)["id"],"yes",sid)
    service.flush()  # delivers the caregiver email


def approve(service,sid="approval-sid"):
    """Both approvals: the senior's exact yes, then the caregiver's emailed approval."""
    senior_yes(service,sid)
    return service.caregiver_decide(service.mailer.token,"approve")


def test_real_http_provider_persists_idempotent_receipt_and_marks_invoice_paid(provider):
    client,process = provider
    assert process.pid != os.getpid()
    invoice = client.invoices("demo-margaret")[0]
    key = uuid4().hex
    assert client.receipt(key) is None
    receipt = client.pay(key,payload(invoice))
    assert client.pay(key,payload(invoice)) == receipt
    assert client.receipt(key) == receipt
    assert client.invoice(invoice["id"])["status"] == "paid"
    all_receipts = client._request("GET","/v1/payments")["payments"]
    assert len(all_receipts) == 1
    changed = payload(invoice)
    changed["amount_cents"] += 1
    with pytest.raises(BillerError,match="409"):
        client.pay(key,changed)
    with pytest.raises(BillerError,match="409"):
        client.pay(uuid4().hex,payload(invoice))


def test_provider_requires_auth_and_exact_payment_contract(provider):
    client,_ = provider
    assert requests.get(client.base+"/v1/invoices",timeout=3).status_code == 401
    invoice = client.invoices("demo-margaret")[0]
    for field,value in (("amount_cents",True),("customer_ref","someone-else"),
                        ("invoice_version",2),("simulation_date","2027-01-01")):
        body = payload(invoice)
        body[field] = value
        with pytest.raises(BillerError):
            client.pay(uuid4().hex,body)
    assert client.invoice(invoice["id"])["status"] == "unpaid"
    html = requests.get(client.base,timeout=3).text
    assert "Demo Utility Company" in html and BILLER_TOKEN not in html


def test_original_signed_whatsapp_flow_reaches_independent_biller(provider,tmp_path,settings):
    client,_ = provider
    service,bank,twilio = setup_service(tmp_path,settings,client)
    initial = job(service)
    assert initial["status"] == "awaiting_user" and initial["readback_delivered"]
    assert "is due Wednesday, Oct 28" in twilio.sent[0][1] and "Would you like me to schedule it?" in twilio.sent[0][1]
    assert client.invoice(initial["invoice_id"])["status"] == "unpaid"
    llm = FakeLLM(intent={"intent":"yes","confidence":0.99,"needs":[],"restatement":""})
    app = create_app(settings,agent=Agent(llm,bank,"Margaret",payments=service),
                     twilio=twilio,payments=service,executor=InlineExecutor())
    data = form("YES "+initial["id"],sid="SMexternal-approved")
    for _ in range(2):
        response = app.test_client().post("/whatsapp/inbound",data=data,
                                         headers={"X-Twilio-Signature":sign(data)})
        assert response.status_code == 200 and b"<Response" in response.data
    assert job(service)["status"] == "awaiting_caregiver"
    assert client._request("GET","/v1/payments")["payments"] == []
    assert any("Caregiver approval is pending" in text for _,text in twilio.sent)
    token = service.mailer.token
    assert len(service.mailer.sent) == 1
    for _ in range(2):
        page = app.test_client().post(f"/caregiver/{token}/approve")
        assert page.status_code == 200
    updated = job(service)
    assert updated["status"] == "paid"
    assert client.invoice(initial["invoice_id"])["status"] == "paid"
    assert len(client._request("GET","/v1/payments")["payments"]) == 1
    assert any(updated["receipt"]["id"] in text for _,text in twilio.sent)
    assert bank.balance == 2213  # no pretend Nessie debit
    llm.intent = {"intent":"question","confidence":0.99,"needs":["bills"],"restatement":"bill status"}
    summary = Agent(llm,bank,"Margaret",payments=service).respond("Did you pay my bill?")
    assert "Austin Energy" in summary[0] and "paid" in summary[0] and updated["receipt"]["id"] in summary[0]
    assert "demo" not in summary[0].casefold() and "simulated" not in summary[0].casefold()
    assert "simulator" not in summary[0].casefold()
    llm.intent = {"intent":"question","confidence":0.99,"needs":["balance"],"restatement":"checking balance"}
    llm.reply = {"messages": []}
    balance = Agent(llm,bank,"Margaret",payments=service).respond("What's my balance?")
    assert balance == ["Your checking balance is $2,093.00, after paying $120.00 to Austin Energy."]
    assert bank.balance == 2213


def test_lost_http_response_reconciles_without_second_payment(provider,tmp_path,settings):
    client,_ = provider
    service,_,_ = setup_service(tmp_path,settings,client)
    approve(service)
    original = client.pay
    def accept_then_lose_reply(key,body):
        original(key,body)
        raise BillerError("Simulated lost reply")
    client.pay = accept_then_lose_reply
    service.tick()
    assert job(service)["status"] == "reconciling"
    assert client.invoice(job(service)["invoice_id"])["status"] == "paid"
    client.pay = lambda *a,**kw: pytest.fail("Receipt lookup must avoid another POST")
    service.tick()
    assert job(service)["status"] == "paid"
    assert len(client._request("GET","/v1/payments")["payments"]) == 1


def test_restart_recovers_from_provider_acceptance_before_local_commit(provider,tmp_path,settings):
    client,_ = provider
    service,bank,twilio = setup_service(tmp_path,settings,client)
    approve(service)
    pending = job(service)
    # Provider accepted but the Steady process died before persisting success.
    client.pay(pending["idempotency_key"],{**payload(client.invoice(pending["invoice_id"])),
                                          "simulation_date":pending["scheduled_for"]})
    restarted = PaymentService(service.database,client,bank,twilio,MOM,demo_date=ANCHOR,mailer=FakeMailer())
    restarted.tick()
    assert job(restarted)["status"] == "paid"
    assert len(client._request("GET","/v1/payments")["payments"]) == 1


def test_same_provider_key_and_fixed_body_are_reused_after_pre_acceptance_failure(provider,tmp_path,settings):
    client,_ = provider
    service,_,_ = setup_service(tmp_path,settings,client)
    approve(service)
    original,key = client.pay,job(service)["idempotency_key"]
    client.pay = lambda *a: (_ for _ in ()).throw(BillerError("Network unavailable"))
    service.tick()
    assert job(service)["status"] == "reconciling" and client.receipt(key) is None
    client.pay = original
    service.tick()
    assert job(service)["status"] == "paid" and job(service)["idempotency_key"] == key


def test_fresh_nessie_funds_check_prevents_external_submission(provider,tmp_path,settings):
    client,_ = provider
    service,bank,_ = setup_service(tmp_path,settings,client)
    approve(service)
    bank.balance = 210
    service.tick()
    assert job(service)["status"] == "held"
    assert client.invoice(job(service)["invoice_id"])["status"] == "unpaid"
    assert client._request("GET","/v1/payments")["payments"] == []


def test_future_income_can_plan_but_cannot_execute_until_settled(provider,tmp_path,settings):
    client,_ = provider
    bank = FakeNessie(balance=300,bills=[],deposits=[{"status":"pending","amount":1000,"transaction_date":"2026-10-26"}])
    service,bank,_ = setup_service(tmp_path,settings,client,bank)
    # All three external bills are reserved: $300 - $255.24 breaches the buffer.
    assert job(service)["scheduled_for"] == "2026-10-26"
    approve(service)
    service.advance(1)
    assert job(service)["status"] == "held"
    assert client._request("GET","/v1/payments")["payments"] == []


def test_cancel_before_scheduled_day_never_calls_provider(provider,tmp_path,settings):
    client,_ = provider
    bank = FakeNessie(balance=300,bills=[],deposits=[{"status":"pending","amount":1000,"transaction_date":"2026-10-26"}])
    service,bank,_ = setup_service(tmp_path,settings,client,bank)
    approve(service)
    service.handle("CANCEL "+job(service)["id"],"undo","cancel-sid")
    bank.balance = 1300
    service.advance(1)
    assert job(service)["status"] == "cancelled"
    assert client._request("GET","/v1/payments")["payments"] == []


def test_wrong_sender_and_missing_sid_are_rejected_even_when_allow_any_sender(provider,tmp_path,settings):
    from dataclasses import replace
    client,_ = provider
    settings = replace(settings,allow_any_sender=True)
    service,bank,twilio = setup_service(tmp_path,settings,client)
    llm = FakeLLM(intent={"intent":"yes","confidence":0.99,"needs":[],"restatement":""})
    app = create_app(settings,agent=Agent(llm,bank,"Margaret",payments=service),
                     payments=service,twilio=twilio,executor=InlineExecutor())
    wrong = form("YES "+job(service)["id"],from_="whatsapp:+15550009999")
    assert app.test_client().post("/whatsapp/inbound",data=wrong,headers={"X-Twilio-Signature":sign(wrong)}).status_code == 200
    blank = form("YES "+job(service)["id"],sid="")
    assert app.test_client().post("/whatsapp/inbound",data=blank,headers={"X-Twilio-Signature":sign(blank)}).status_code == 400
    assert job(service)["user_approved"] is False


def test_readback_and_lexical_yes_are_required(provider,tmp_path,settings):
    client,_ = provider
    service,_,transport = setup_service(tmp_path,settings,client)
    for i,text in enumerate(("yes 00000000", "maybe yes "+job(service)["id"], "forwarded: YES "+job(service)["id"],
                             "yes I guess, pay whatever")):
        service.handle(text,"yes",f"ambiguous-{i}")
        assert job(service)["status"] == "awaiting_user"
    state = service.state()
    assert state["jobs"][job(service)["id"]]["user_approved"] is False


def test_operator_routes_are_authenticated_and_clock_is_opt_in(provider,tmp_path,settings):
    client,_ = provider
    service,bank,transport = setup_service(tmp_path,settings,client)
    app = create_app(settings,agent=Agent(FakeLLM(),bank,"Margaret"),twilio=transport)
    register_controls(app,service,CONTROL_TOKEN)
    http = app.test_client()
    assert http.get("/demo/payments").status_code == 401
    assert http.post("/demo/payments/advance",json={"days":2}).status_code == 401
    headers={"Authorization":"Bearer "+CONTROL_TOKEN}
    assert http.get("/demo/payments",headers=headers).json["simulation"] is True
    assert http.post("/demo/payments/advance",headers=headers,json={"days":True}).status_code == 400
    assert http.post("/demo/payments/advance",headers=headers,json={"days":2}).status_code == 200


def test_biller_client_refuses_redirects_insecure_remote_origins_and_secret_errors(provider):
    with pytest.raises(ValueError):
        BillerClient("http://outside.example",BILLER_TOKEN)
    with pytest.raises(ValueError):
        BillerClient("https://user:password@outside.example",BILLER_TOKEN)
    client,_ = provider
    client.token = "incorrect-token"
    with pytest.raises(BillerError) as exc:
        client.invoices("demo-margaret")
    assert BILLER_TOKEN not in str(exc.value) and "incorrect-token" not in str(exc.value)


def test_changed_provider_invoice_needs_new_approval(provider,tmp_path,settings):
    client,_ = provider
    service,_,_ = setup_service(tmp_path,settings,client)
    approve(service)
    with sqlite3.connect(tmp_path/"provider.sqlite3") as db:
        db.execute("UPDATE invoices SET amount_cents=12100,version=2 WHERE id=?",(job(service)["invoice_id"],))
    service.tick()
    assert job(service)["status"] == "held"
    assert client._request("GET","/v1/payments")["payments"] == []


def test_same_day_pending_income_is_not_treated_as_cleared_cash(provider,tmp_path,settings):
    client,_ = provider
    bank = FakeNessie(balance=300,bills=[],deposits=[{"status":"pending","amount":1000,"transaction_date":ANCHOR.isoformat()}])
    service,_,_ = setup_service(tmp_path,settings,client,bank)
    assert job(service)["status"] == "held" and job(service)["scheduled_for"] is None


def test_future_execution_succeeds_only_when_fresh_nessie_balance_reflects_income(provider,tmp_path,settings):
    client,_ = provider
    bank = FakeNessie(balance=300,bills=[],deposits=[{"status":"pending","amount":1000,"transaction_date":"2026-10-26"}])
    service,bank,_ = setup_service(tmp_path,settings,client,bank)
    approve(service)
    bank.balance = 1300
    bank.deposits[0]["status"] = "completed"
    service.advance(1)
    assert job(service)["status"] == "paid"
    assert client.receipt(job(service)["idempotency_key"])["simulation_date"] == "2026-10-26"


def test_available_balance_ignores_unpaid_and_subtracts_paid(provider,tmp_path,settings):
    client,_ = provider
    service,bank,_ = setup_service(tmp_path,settings,client)
    facts = {"balance":{"status":"ok","amount":"$2,213.00"}}
    account_id = bank.resolve("Margaret")["_id"]
    waiting = service.with_available_balance(account_id, facts)
    assert waiting["balance"]["amount"] == "$2,213.00"
    assert "after_payments" not in waiting["balance"]
    approve(service)
    service.tick()
    paid = service.with_available_balance(account_id, facts)
    assert paid["balance"]["amount"] == "$2,093.00"
    assert paid["balance"]["after_payments"] == [{"payee":"Austin Energy","amount":"$120.00"}]
    assert bank.balance == 2213


def test_accepted_simulated_payments_are_reserved_against_unchanged_bank_balance(provider,tmp_path,settings):
    client,_ = provider
    service,bank,_ = setup_service(tmp_path,settings,client)
    approve(service)
    service.tick()
    bank.balance = 350
    service.advance(2)
    water = next(j for j in service.state()["jobs"].values() if j["payee"] == "Water Co")
    assert water["status"] == "held"
    assert len(client._request("GET","/v1/payments")["payments"]) == 1


def test_new_or_large_payee_is_held_in_this_integration(provider,tmp_path,settings):
    client,_ = provider
    with sqlite3.connect(tmp_path/"provider.sqlite3") as db:
        db.execute("UPDATE invoices SET payee='Unknown Roofer',amount_cents=50000 WHERE due_date='2026-10-28'")
    service,_,_ = setup_service(tmp_path,settings,client)
    assert job(service)["status"] == "held"
    assert client._request("GET","/v1/payments")["payments"] == []


def test_opt_in_original_app_factory_registers_payment_controls_without_endpoint_collision(provider,tmp_path,settings,monkeypatch):
    from backend.payments import integration
    client,_ = provider
    service,bank,transport = setup_service(tmp_path,settings,client)
    monkeypatch.setenv("STEADY_EXTERNAL_BILLER","1")
    monkeypatch.setattr(integration,"build_payments",lambda *args:(service,CONTROL_TOKEN))
    app = create_app(settings,agent=Agent(FakeLLM(),bank,"Margaret",payments=service),
                     twilio=transport,executor=InlineExecutor())
    assert app.extensions["external_payments"] is service
    assert app.test_client().get("/demo/payments",headers={"Authorization":"Bearer "+CONTROL_TOKEN}).status_code == 200
    assert "/whatsapp/status" in {rule.rule for rule in app.url_map.iter_rules()}


def test_provider_records_and_updates_a_photo_invoice(provider):
    client, _ = provider
    created = client.create_invoice("demo-margaret", "Crawford Electric Cooperative", 11800, "2027-05-15")
    assert created["payee"] == "Crawford Electric Cooperative"
    assert created["amount_cents"] == 11800
    assert created["due_date"] == "2027-05-15"
    assert created["status"] == "unpaid"
    assert any(b["id"] == created["id"] for b in client.invoices("demo-margaret"))
    updated = client.create_invoice("demo-margaret", "Crawford Electric Cooperative", 11900, "2027-05-16")
    assert updated["id"] == created["id"]
    assert updated["amount_cents"] == 11900
    assert updated["due_date"] == "2027-05-16"
    assert updated["version"] == created["version"] + 1


def test_bill_summary_includes_nessie_bills_and_does_not_say_demo(provider, tmp_path, settings):
    client, _ = provider
    service, bank, _ = setup_service(tmp_path, settings, client)
    bank.bills.append({
        "_id": "bill-crawford", "payee": "Crawford Electric Cooperative",
        "payment_amount": 118.0, "payment_date": "2027-05-15", "status": "pending",
    })
    text = service.bill_summary()[0]
    assert "Crawford Electric Cooperative" in text and "$118.00" in text and "May 15" in text
    assert "Austin Energy" in text and "Water Co" in text and "Spectrum" in text
    assert "demo" not in text.casefold() and "simulated" not in text.casefold()
    assert "simulator" not in text.casefold()


def test_photo_yes_writes_nessie_so_later_bill_list_includes_it(provider, tmp_path, settings):
    from backend.agent.photo import PhotoIntake
    from tests.test_photo import CLOCK, MEDIA, PNG, photo_llm

    client, _ = provider
    bank = FakeNessie(balance=2213, bills=[])
    service, bank, _ = setup_service(tmp_path, settings, client, bank)
    llm = photo_llm(payee="Crawford Electric Cooperative", amount="$118.00", due_date="May 15")
    photos = PhotoIntake(llm, bank, "Margaret", fetch=lambda url: (PNG, "image/jpeg"),
                         clock=lambda: CLOCK, payments=service)
    agent = Agent(llm, bank, "Margaret", payments=service, photos=photos)
    first = agent.respond("", media=MEDIA)
    assert "Crawford Electric Cooperative" in first[0] and "YES" in first[0]
    assert bank.writes == []
    saved = agent.respond("YES")
    assert "Saved. Crawford Electric Cooperative" in saved[0]
    assert bank.writes[0][0] == "create"
    created = next(b for b in bank.bills if b["payee"] == "Crawford Electric Cooperative")
    assert created["payment_amount"] == 118.0
    assert created["status"] == "pending"
    invoices = client.invoices("demo-margaret")
    assert any(b["payee"] == "Crawford Electric Cooperative" and b["amount_cents"] == 11800
               and b["status"] == "unpaid" for b in invoices)
    llm.intent = {"intent": "question", "confidence": 0.99, "needs": ["bills"],
                  "restatement": "the bills she has"}
    listed = agent.respond("What are the bills I have")
    assert "Crawford Electric Cooperative" in listed[0] and "$118.00" in listed[0]
    assert "Austin Energy" in listed[0]
    assert "demo" not in listed[0].casefold()
    assert "simulated" not in listed[0].casefold()
