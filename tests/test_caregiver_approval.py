"""Two-party approval: the senior's yes plus the caregiver's emailed decision.

Uses the same separate biller process as test_external_biller. No real email,
WhatsApp, or bank calls are made.
"""

import sqlite3

from backend.agent.loop import Agent
from backend.app import create_app
from backend.payments.caregiver import CaregiverMailer, MailError
from backend.payments.service import MAX_EMAIL_ATTEMPTS, PaymentService
from tests.conftest import MOM, FakeLLM, FakeNessie
from tests.test_app import InlineExecutor, RecordingTwilio
from tests.test_external_biller import (ANCHOR, FakeMailer, approve, job, provider,  # noqa: F401
                                        senior_yes, setup_service)


def payments(client):
    return client._request("GET","/v1/payments")["payments"]


def app_for(settings,service,bank,twilio):
    return create_app(settings,agent=Agent(FakeLLM(),bank,"Margaret",payments=service),
                      twilio=twilio,payments=service,executor=InlineExecutor()).test_client()


def test_remind_sends_the_open_yes_or_no_without_a_hi(provider,tmp_path,settings):
    client,_ = provider
    service,_,twilio = setup_service(tmp_path,settings,client)
    before = len(twilio.sent)
    assert service.remind() is True
    service.flush()
    assert len(twilio.sent) == before + 1
    assert "Would you like me to schedule it? Reply YES or NO." in twilio.sent[-1][1]


def test_fresh_proposal_is_offered_once_even_if_startup_reminds(provider,tmp_path,settings):
    client,_ = provider
    transport = RecordingTwilio(settings)
    transport.record_inbound(MOM)
    service = PaymentService(tmp_path/"once.sqlite3",client,FakeNessie(balance=2213,bills=[]),
                             transport,MOM,demo_date=ANCHOR,mailer=FakeMailer(),caregiver_name="Sarah")
    service.tick()
    assert service.remind() is False
    service.flush()
    proposals = [body for _,body in transport.sent if "Reply YES or NO" in body]
    assert len(proposals) == 1


def test_hi_then_flush_does_not_resend_a_queued_proposal(provider,tmp_path,settings):
    client,_ = provider
    bank = FakeNessie(balance=2213,bills=[])
    transport = RecordingTwilio(settings)
    transport.record_inbound(MOM)
    service = PaymentService(tmp_path/"hi-once.sqlite3",client,bank,transport,MOM,demo_date=ANCHOR,
                             mailer=FakeMailer(),caregiver_name="Sarah")
    service.tick()
    out = Agent(FakeLLM(),bank,"Margaret",payments=service).respond("hi")
    service.flush()
    assert "Reply YES or NO" in out[0]
    assert [body for _,body in transport.sent if "Reply YES or NO" in body] == []


def test_hi_repeats_the_open_yes_or_no_proposal(provider,tmp_path,settings):
    client,_ = provider
    service,bank,_ = setup_service(tmp_path,settings,client)
    out = Agent(FakeLLM(),bank,"Margaret",payments=service).respond("hi")
    assert out == [service.pending_readback()]
    assert "Would you like me to schedule it? Reply YES or NO." in out[0]


def test_recommends_paying_after_deposit_and_caregiver_email_hides_balance(provider,tmp_path,settings):
    client,_ = provider
    with sqlite3.connect(tmp_path/"provider.sqlite3") as db:
        db.execute("UPDATE invoices SET amount_cents=60000 WHERE payee='Austin Energy'")
    bank = FakeNessie(balance=700,bills=[],deposits=[{"status":"pending","amount":1000,"transaction_date":"2026-10-27"}])
    service,bank,twilio = setup_service(tmp_path,settings,client,bank)
    proposal = twilio.sent[0][1]
    assert proposal.startswith("Your $600.00 Austin Energy bill is due Wednesday, Oct 28.")
    assert "I recommend paying it Tuesday, Oct 27 after your deposit arrives" in proposal
    assert "Would you like me to schedule it? Reply YES or NO." in proposal

    senior_yes(service)
    payment,token = service.mailer.sent[0]
    assert payment == {"payee":"Austin Energy","amount":"$600.00","date":"Tuesday, Oct 27"}
    mailer = CaregiverMailer("steady@example.com","abcd efgh ijkl mnop","sarah@example.com",
                             base_url="https://steady.example",caregiver_name="Sarah")
    message = mailer.compose(payment,token)
    text = message.get_body(("plain",)).get_content() + message.get_body(("html",)).get_content()
    for secret in ("$700", "$1,000", "$200", "balance", "deposit", "cushion"):
        assert secret not in text
    assert f"https://steady.example/caregiver/{token}?decision=approve" in text and "decision=deny" in text
    assert payments(client) == []


def test_cosigner_alert_email_hides_balance():
    built = []

    class Capture:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self, context=None):
            pass
        def login(self, *a):
            pass
        def send_message(self, message):
            built.append(message)

    mailer = CaregiverMailer("steady@example.com", "abcd efgh ijkl mnop", "sarah@example.com",
                             base_url="https://steady.example", smtp_factory=lambda: Capture())
    mailer.notify("unexpected duplicate",
                  {"merchant": "Dentist", "amount": "$120", "date": "Oct 24", "what_she_said": "No",
                   "balance": "$700"}, "now")
    body = built[0].get_body(("plain",)).get_content() + built[0].get_body(("html",)).get_content()
    assert "Dentist" in body and "$120" in body and "Oct 24" in body
    assert "$700" not in body
    assert "No account balance is included" in body


def test_bare_yes_applies_only_to_the_single_open_proposal(provider,tmp_path,settings):
    client,_ = provider
    service,_,_ = setup_service(tmp_path,settings,client)
    service.handle("yes","yes","bare-yes")
    assert job(service)["status"] == "awaiting_caregiver"
    service.handle("Yes","yes","second-yes")
    assert len([i for i in service.state()["outbox"] if i["kind"] == "caregiver_email"]) == 1


def test_word_and_intent_must_agree(provider,tmp_path,settings):
    client,_ = provider
    service,_,_ = setup_service(tmp_path,settings,client)
    service.handle("no","yes","mismatch-1")
    service.handle("yes","no","mismatch-2")
    assert job(service)["status"] == "awaiting_user" and job(service)["user_approved"] is False


def test_payment_runs_only_after_both_approvals(provider,tmp_path,settings):
    client,_ = provider
    service,_,twilio = setup_service(tmp_path,settings,client)
    senior_yes(service)
    service.tick()
    assert job(service)["status"] == "awaiting_caregiver" and payments(client) == []
    view,_ = service.caregiver_decide(service.mailer.token,"approve")
    assert view["status"] == "scheduled"
    service.tick()
    service.flush()
    assert job(service)["status"] == "paid" and len(payments(client)) == 1
    assert twilio.sent[-1][1].startswith("Done. Austin Energy confirmed your $120.00 payment.")


def test_caregiver_denial_stops_the_payment(provider,tmp_path,settings):
    client,_ = provider
    service,_,twilio = setup_service(tmp_path,settings,client)
    senior_yes(service)
    service.caregiver_decide(service.mailer.token,"deny")
    service.caregiver_decide(service.mailer.token,"approve")  # a later click cannot revive it
    service.tick()
    service.flush()
    assert job(service)["status"] == "denied" and payments(client) == []
    assert "Sarah did not approve the Austin Energy payment" in twilio.sent[-1][1]


def test_silence_is_never_consent(provider,tmp_path,settings):
    client,_ = provider
    bank = FakeNessie(balance=300,bills=[],deposits=[{"status":"pending","amount":1000,"transaction_date":"2026-10-26"}])
    service,bank,twilio = setup_service(tmp_path,settings,client,bank)
    senior_yes(service)
    bank.balance = 1300
    service.advance(1)
    service.flush()
    assert job(service)["status"] == "awaiting_caregiver" and payments(client) == []
    assert "still waiting for Sarah" in twilio.sent[-1][1]
    service.advance(3)
    service.flush()
    assert job(service)["status"] == "expired" and payments(client) == []


def test_senior_can_cancel_while_caregiver_decides(provider,tmp_path,settings):
    client,_ = provider
    service,bank,twilio = setup_service(tmp_path,settings,client)
    senior_yes(service)
    service.handle("no","no","cancel-sid")
    view,_ = service.caregiver_decide(service.mailer.token,"approve")
    service.tick()
    assert view["status"] == "cancelled" and payments(client) == []
    page = app_for(settings,service,bank,twilio).get(f"/caregiver/{service.mailer.token}")
    assert b"cancelled" in page.data and b"<form" not in page.data


def test_opening_the_link_never_decides(provider,tmp_path,settings):
    client,_ = provider
    service,bank,twilio = setup_service(tmp_path,settings,client)
    senior_yes(service)
    http = app_for(settings,service,bank,twilio)
    token = service.mailer.token
    page = http.get(f"/caregiver/{token}?decision=approve")
    assert page.status_code == 200 and b"Austin Energy" in page.data and b"$120.00" in page.data
    assert b"2,213" not in page.data and b"balance" not in page.data.lower()
    assert job(service)["status"] == "awaiting_caregiver"
    assert http.get("/caregiver/"+"x"*43).status_code == 404
    assert http.post("/caregiver/"+"x"*43+"/approve").status_code == 404
    assert http.post(f"/caregiver/{token}/maybe").status_code == 404
    assert job(service)["status"] == "awaiting_caregiver" and payments(client) == []


def test_late_caregiver_approval_does_not_pay(provider,tmp_path,settings):
    client,_ = provider
    service,_,twilio = setup_service(tmp_path,settings,client)
    senior_yes(service)
    service.advance(1)  # planned date was the anchor day
    service.caregiver_decide(service.mailer.token,"approve")
    service.tick()
    service.flush()
    assert job(service)["status"] == "held" and payments(client) == []
    assert "after the planned day" in twilio.sent[-1][1]


def test_email_failures_retry_then_hold_without_consent(provider,tmp_path,settings):
    client,_ = provider
    mailer = FakeMailer()
    mailer.fail = True
    service,_,twilio = setup_service(tmp_path,settings,client,mailer=mailer)
    service.handle("YES "+job(service)["id"],"yes","yes-sid")
    for _ in range(MAX_EMAIL_ATTEMPTS):
        service.flush()
    assert job(service)["caregiver_email_attempts"] == MAX_EMAIL_ATTEMPTS
    assert "couldn't reach Sarah by email" in twilio.sent[-1][1]
    assert job(service)["status"] == "awaiting_caregiver" and payments(client) == []
    assert all(item.get("token") is None for item in service.state()["outbox"] if item["kind"] == "caregiver_email")


class FakeSMTP:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self.fail:
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"bad password for steady@example.com")

    def send_message(self, message):
        self.calls.append(("send", message["To"]))


def test_gmail_mailer_uses_tls_and_hides_smtp_error_details():
    smtp = FakeSMTP()
    mailer = CaregiverMailer("steady@example.com","abcd efgh ijkl mnop","sarah@example.com",
                             base_url="http://127.0.0.1:5000",smtp_factory=lambda: smtp)
    mailer.request_approval({"payee":"Austin Energy","amount":"$120.00","date":"Sunday, Oct 25"},"t"*43)
    assert smtp.calls == ["starttls",("login","steady@example.com","abcdefghijklmnop"),("send","sarah@example.com")]
    assert "abcd" not in repr(mailer)
    broken = CaregiverMailer("steady@example.com","x","sarah@example.com",base_url="http://127.0.0.1:5000",
                             smtp_factory=lambda: FakeSMTP(fail=True))
    try:
        broken.request_approval({"payee":"A","amount":"$1.00","date":"d"},"t"*43)
    except MailError as exc:
        assert "steady@example.com" not in str(exc) and "SMTPAuthenticationError" in str(exc)
    else:
        raise AssertionError("expected MailError")
