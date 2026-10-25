from datetime import date

from backend.agent import reply
from backend.agent.loop import Agent
from backend.agent.photo import (
    ALLOWED_HOSTS,
    DraftBill,
    InboundMedia,
    MediaError,
    PhotoIntake,
    extract_draft,
    fetch_twilio_media,
    parse_bill_amount_cents,
    parse_due_date,
    simple_decision,
)
from tests.conftest import FakeLLM, FakeNessie
from tests.test_app import InlineExecutor, RecordingTwilio, form, sign
from tests.test_loop import intent

from backend.app import create_app

CLOCK = date(2026, 10, 25)
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)
PHOTO = {
    "is_bill": True,
    "payee": "Austin Energy",
    "amount": "$130.00",
    "due_date": "Oct 31, 2026",
    "confidence": 0.96,
}
MEDIA = InboundMedia("https://api.twilio.com/2010-04-01/Accounts/ACdummy/Messages/MM1/Media/ME1",
                     "image/jpeg")


def photo_llm(**fields):
    return FakeLLM(intent=intent("unclear"), photo={**PHOTO, **fields})


def intake(llm=None, nessie=None, fetch=None):
    return PhotoIntake(
        llm or photo_llm(),
        nessie or FakeNessie(),
        "Margaret",
        fetch=fetch or (lambda url: (PNG, "image/jpeg")),
        clock=lambda: CLOCK,
    )


def agent_with_photos(llm=None, nessie=None):
    bank = nessie or FakeNessie()
    model = llm or photo_llm()
    return Agent(model, bank, "Margaret", photos=intake(model, bank)), bank


def test_short_yes_no_does_not_need_the_model():
    assert simple_decision("yes") == "yes"
    assert simple_decision("OK.") == "yes"
    assert simple_decision("no thanks") == "no"
    assert simple_decision("undo that") == "no"
    assert simple_decision("what bills do I have") is None


def test_amount_parser_accepts_printed_and_bare_numbers():
    assert parse_bill_amount_cents("$130.00") == 13000
    assert parse_bill_amount_cents("45.25") == 4525
    assert parse_bill_amount_cents("1,234.50") == 123450
    assert parse_bill_amount_cents("") is None
    assert parse_bill_amount_cents("$0") is None
    assert parse_bill_amount_cents("$99999") is None


def test_due_date_parser_fills_year_and_rejects_junk():
    assert parse_due_date("Oct 31, 2026", clock=CLOCK) == date(2026, 10, 31)
    assert parse_due_date("10/31/2026", clock=CLOCK) == date(2026, 10, 31)
    assert parse_due_date("2026-11-03", clock=CLOCK) == date(2026, 11, 3)
    assert parse_due_date("Nov 3", clock=CLOCK) == date(2026, 11, 3)
    assert parse_due_date("Jan 5", clock=CLOCK) == date(2027, 1, 5)
    assert parse_due_date("not a date", clock=CLOCK) is None
    assert parse_due_date("Feb 31, 2026", clock=CLOCK) is None


def test_extract_rejects_guesses_and_non_bills():
    clock = CLOCK
    assert extract_draft(photo_llm(is_bill=False), PNG, "image/jpeg", clock=clock) is None
    assert extract_draft(photo_llm(confidence=0.2), PNG, "image/jpeg", clock=clock) is None
    assert extract_draft(photo_llm(amount=""), PNG, "image/jpeg", clock=clock) is None
    assert extract_draft(photo_llm(due_date=""), PNG, "image/jpeg", clock=clock) is None
    draft = extract_draft(photo_llm(), PNG, "image/jpeg", clock=clock)
    assert draft == DraftBill("Austin Energy", 13000, date(2026, 10, 31))


def test_photo_asks_for_yes_and_does_not_write_yet():
    bank = FakeNessie()
    helper = intake(photo_llm(), bank)
    out = helper.ingest(MEDIA)
    assert "Austin Energy" in out[0] and "$130.00" in out[0] and "Oct 31" in out[0]
    assert "YES" in out[0]
    assert bank.writes == []
    assert helper.pending() is not None


def test_yes_updates_the_matching_pending_nessie_bill():
    bank = FakeNessie()
    helper = intake(photo_llm(), bank)
    helper.ingest(MEDIA)
    out = helper.decide("yes")
    assert out == ["Updated. Austin Energy is now $130.00 due Oct 31."]
    assert ("update", "bill-ae") == bank.writes[0][:2]
    saved = next(b for b in bank.bills if b["_id"] == "bill-ae")
    assert saved["payment_amount"] == 130.0
    assert saved["payment_date"] == "2026-10-31"
    assert saved["status"] == "pending"
    assert helper.pending() is None


def test_yes_creates_a_new_nessie_bill_for_an_unknown_payee():
    bank = FakeNessie()
    helper = intake(photo_llm(payee="City Garbage", amount="32.10", due_date="Nov 8, 2026"), bank)
    helper.ingest(MEDIA)
    out = helper.decide("yes")
    assert out == ["Saved. City Garbage for $32.10 is due Nov 8."]
    assert bank.writes[0][0] == "create"
    created = next(b for b in bank.bills if b["payee"] == "City Garbage")
    assert created["payment_amount"] == 32.10
    assert created["payment_date"] == "2026-11-08"
    assert created["status"] == "pending"


def test_no_discards_without_writing():
    bank = FakeNessie()
    helper = intake(photo_llm(), bank)
    helper.ingest(MEDIA)
    assert helper.decide("no") == [reply.PHOTO_DISCARD]
    assert bank.writes == []
    assert helper.pending() is None


def test_unreadable_photo_does_not_ask_for_yes():
    bank = FakeNessie()
    helper = intake(photo_llm(is_bill=False), bank)
    assert helper.ingest(MEDIA) == [reply.PHOTO_UNREADABLE]
    assert helper.pending() is None
    assert bank.writes == []


def test_agent_photo_then_yes_writes_to_nessie():
    agent, bank = agent_with_photos()
    first = agent.respond("", media=MEDIA)
    assert "Reply YES" in first[0]
    assert bank.writes == []
    second = agent.respond("yes")
    assert "Updated. Austin Energy" in second[0]
    assert bank.writes[0][0] == "update"


def test_agent_unclear_repeats_the_photo_question():
    agent, _ = agent_with_photos(FakeLLM(intent=intent("unclear"), photo=PHOTO))
    agent.respond("", media=MEDIA)
    out = agent.respond("hmm")
    assert "Austin Energy" in out[0] and "YES" in out[0]


def test_yes_without_a_photo_stays_unavailable():
    agent, bank = agent_with_photos(FakeLLM(intent=intent("yes"), photo=PHOTO))
    assert agent.respond("yes") == [reply.NOT_AVAILABLE]
    assert bank.writes == []


def test_avif_and_octet_stream_are_treated_as_photos():
    from backend.agent.photo import PhotoIntake as Intake
    bank = FakeNessie()
    helper = Intake(
        photo_llm(), bank, "Margaret",
        fetch=lambda url: (PNG, "application/octet-stream"),
        clock=lambda: CLOCK,
    )
    avif = InboundMedia("https://api.twilio.com/2010-04-01/Accounts/ACdummy/Messages/MM1/Media/bill.avif",
                        "image/avif")
    out = helper.ingest(avif)
    assert "Austin Energy" in out[0] and "YES" in out[0]


def test_photo_without_intake_says_not_on():
    out = Agent(FakeLLM(), FakeNessie(), "Margaret").respond("bill", media=MEDIA)
    assert out == [reply.PHOTO_NOT_ON]


def test_fetch_rejects_non_twilio_hosts():
    try:
        fetch_twilio_media("https://evil.example/bill.jpg", "ACdummy", "token")
    except MediaError:
        pass
    else:
        raise AssertionError("non-Twilio hosts must be rejected")
    assert "api.twilio.com" in ALLOWED_HOSTS


def test_fetch_uses_basic_auth_and_https_twilio_host():
    class Resp:
        ok = True
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}

        def iter_content(self, _size):
            yield PNG

    class OkSession:
        def get(self, url, auth=None, timeout=None, stream=True):
            assert auth == ("ACdummy", "token")
            return Resp()

    image, mime = fetch_twilio_media(MEDIA.url, "ACdummy", "token", session=OkSession())
    assert image == PNG and mime == "image/jpeg"


def test_webhook_photo_then_yes_updates_nessie(settings):
    bank = FakeNessie()
    llm = FakeLLM(intent=intent("yes"), photo=PHOTO)
    helper = intake(llm, bank)
    twilio = RecordingTwilio(settings)
    app = create_app(settings, agent=Agent(llm, bank, "Margaret", photos=helper),
                     twilio=twilio, executor=InlineExecutor())
    client = app.test_client()
    p = form(body="", sid="SMphoto")
    p["NumMedia"] = "1"
    p["MediaUrl0"] = MEDIA.url
    p["MediaContentType0"] = "image/jpeg"
    client.post("/whatsapp/inbound", data=p, headers={"X-Twilio-Signature": sign(p)})
    assert twilio.sent and "Reply YES" in twilio.sent[0][1]
    assert bank.writes == []

    yes = form(body="yes", sid="SMyes")
    client.post("/whatsapp/inbound", data=yes, headers={"X-Twilio-Signature": sign(yes)})
    assert any("Updated. Austin Energy" in body for _, body in twilio.sent)
    assert bank.writes[0][0] == "update"
    assert next(b for b in bank.bills if b["_id"] == "bill-ae")["payment_amount"] == 130.0
