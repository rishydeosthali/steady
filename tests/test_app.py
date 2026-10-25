from concurrent.futures import Executor, Future

from twilio.request_validator import RequestValidator

from backend.app import create_app
from backend.agent import reply
from backend.agent.loop import Agent
from backend.twilio_client import SessionClosed, TwilioWhatsApp
from tests.conftest import DUMMY_TOKEN, MOM, FakeLLM, FakeNessie

URL = "http://localhost/whatsapp/inbound"


class InlineExecutor(Executor):
    def submit(self, fn, /, *a, **k):
        f = Future()
        f.set_result(fn(*a, **k))
        return f


class RecordingTwilio(TwilioWhatsApp):
    def __init__(self, settings):
        super().__init__(settings, client=object())
        self.sent: list[tuple[str, str]] = []

    def send_reply(self, to, body):
        if not self.session_open(to):
            raise SessionClosed("closed")
        self.sent.append((to, body))
        return "SMdummy"


def make(settings, label="unclear"):
    llm = FakeLLM(intent={"intent": label, "confidence": 0.95, "needs": [], "restatement": ""})
    twilio = RecordingTwilio(settings)
    app = create_app(settings, agent=Agent(llm, FakeNessie(), "Margaret"), twilio=twilio,
                     executor=InlineExecutor())
    return app.test_client(), twilio


def sign(params, url=URL, token=DUMMY_TOKEN):
    return RequestValidator(token).compute_signature(url, params)


def form(body="hello", from_=MOM, sid="SM1"):
    return {"From": from_, "To": "whatsapp:+15550002222", "Body": body, "MessageSid": sid}


def test_invalid_signature_is_403_and_nothing_sent(settings):
    client, twilio = make(settings)
    r = client.post("/whatsapp/inbound", data=form(), headers={"X-Twilio-Signature": "bogus"})
    assert r.status_code == 403
    assert twilio.sent == []


def test_missing_signature_is_403(settings):
    client, _ = make(settings)
    assert client.post("/whatsapp/inbound", data=form()).status_code == 403


def test_signature_from_wrong_token_is_403(settings):
    client, _ = make(settings)
    p = form()
    r = client.post("/whatsapp/inbound", data=p,
                    headers={"X-Twilio-Signature": sign(p, token="other-token")})
    assert r.status_code == 403


def test_valid_signature_returns_empty_twiml_then_replies_to_sender(settings):
    client, twilio = make(settings)
    p = form("gibberish asdf")
    r = client.post("/whatsapp/inbound", data=p, headers={"X-Twilio-Signature": sign(p)})
    assert r.status_code == 200
    assert r.mimetype == "text/xml" and b"<Response" in r.data and b"<Message" not in r.data
    assert twilio.sent == [(MOM, reply.REASK)]


def test_other_senders_are_ignored(settings):
    client, twilio = make(settings)
    p = form(from_="whatsapp:+15559998888")
    r = client.post("/whatsapp/inbound", data=p, headers={"X-Twilio-Signature": sign(p)})
    assert r.status_code == 200
    assert twilio.sent == []


def test_duplicate_message_sid_replies_once(settings):
    client, twilio = make(settings)
    p = form(sid="SMdup")
    for _ in range(2):
        client.post("/whatsapp/inbound", data=p, headers={"X-Twilio-Signature": sign(p)})
    assert len(twilio.sent) == 1


def test_status_callback_is_204_and_sends_nothing(settings):
    client, twilio = make(settings)
    p = {"MessageSid": "SM1", "MessageStatus": "delivered"}
    url = "http://localhost/whatsapp/status"
    r = client.post("/whatsapp/status", data=p, headers={"X-Twilio-Signature": sign(p, url)})
    assert r.status_code == 204 and r.data == b""
    assert twilio.sent == []


def test_status_callback_bad_signature_is_403(settings):
    client, _ = make(settings)
    r = client.post("/whatsapp/status", data={"MessageSid": "x"},
                    headers={"X-Twilio-Signature": "bogus"})
    assert r.status_code == 403


def test_free_form_refused_without_inbound_session(settings):
    t = TwilioWhatsApp(settings, client=object())
    try:
        t.send_reply(MOM, "hi")
    except SessionClosed:
        pass
    else:
        raise AssertionError("free-form send must be refused with no open session")
