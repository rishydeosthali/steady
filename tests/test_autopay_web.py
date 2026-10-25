import pytest
from twilio.request_validator import RequestValidator

from backend.demo.autopay import AutopayDemo
from backend.demo.runner import DemoWorker
from backend.demo.webapp import create_autopay_app
from backend.demo.whatsapp import WhatsAppBridge, demo_twilio_settings
from tests.conftest import DUMMY_TOKEN, MOM
from tests.test_app import InlineExecutor, RecordingTwilio

TOKEN = "synthetic-demo-control-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def local(tmp_path):
    runtime = AutopayDemo(tmp_path / "demo.sqlite3")
    runtime.tick()
    worker = DemoWorker(runtime)
    app = create_autopay_app(runtime, TOKEN, worker=worker, executor=InlineExecutor())
    return app.test_client(), runtime, worker


def test_ui_and_health_are_available_but_bank_controls_require_token(local):
    client, _, _ = local
    response = client.get("/")
    assert response.status_code == 200 and b"SIMULATED BANK" in response.data
    assert TOKEN.encode() not in response.data
    assert client.get("/static/autopay.js").status_code == 200
    assert client.get("/healthz").json["bank_mode"] == "synthetic_local_simulation"
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/advance", json={"days": 3}).status_code == 401
    assert client.get("/api/state", headers=AUTH).status_code == 200
    assert client.get("/api/state", headers={"Authorization":"Bearer wrong"}).status_code == 401
    assert client.get("/api/state", headers={"Authorization":"Bearer ❌"}).status_code == 401
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_api_full_demo_flow_and_reset(local):
    client, runtime, worker = local
    req = next(iter(runtime.state()["requests"].values()))
    reply = client.post("/api/message", headers=AUTH,
                        json={"actor":"senior", "text":"Yes", "request_id":req["id"], "message_id":"ui-yes"})
    assert reply.json["requests"][req["id"]]["status"] == "scheduled"
    result = client.post("/api/advance", headers=AUTH, json={"days":2})
    assert result.json["requests"][req["id"]]["status"] == "paid"
    assert result.json["balance_cents"] == 118000
    client.post("/api/clock", headers=AUTH, json={"seconds_per_day":10})
    assert worker.rate == 10
    result = client.post("/api/reset", headers=AUTH, json={})
    assert result.json["balance_cents"] == 30000 and worker.rate == 0


@pytest.mark.parametrize("payload", [{"actor":[]}, {"actor":"family", "text":True}, {"actor":"senior", "text":"Yes", "request_id":{}}, {}])
def test_bad_messages_are_400_not_server_errors(local, payload):
    assert local[0].post("/api/message", headers=AUTH, json=payload).status_code == 400


def test_bad_json_and_invalid_clock_and_missing_transport(local):
    client, _, _ = local
    assert client.post("/api/advance", headers=AUTH, data="bad", content_type="application/json").status_code == 400
    assert client.post("/api/clock", headers=AUTH, json={"seconds_per_day":1}).status_code == 400
    assert client.post("/whatsapp/inbound").status_code == 404
    assert client.post("/whatsapp/status").status_code == 404


def whatsapp(tmp_path, settings, *, family="whatsapp:+15550003333", wake=False):
    runtime = AutopayDemo(tmp_path / "whatsapp.sqlite3", external=True)
    runtime.tick()
    transport = RecordingTwilio(settings)
    bridge = WhatsAppBridge(runtime, settings, family, twilio=transport, allow_wakeup=wake)
    app = create_autopay_app(runtime, TOKEN, bridge=bridge, executor=InlineExecutor())
    return app.test_client(), runtime, bridge, transport


def post_signed(client, body, *, sender=MOM, sid="SMtest", signature=None, path="/whatsapp/inbound"):
    params = {"From":sender, "Body":body, "MessageSid":sid}
    signed_url = "http://localhost" + path
    sig = signature or RequestValidator(DUMMY_TOKEN).compute_signature(signed_url, params)
    return client.post(path, data=params, headers={"X-Twilio-Signature":sig})


def test_whatsapp_never_sends_freeform_outside_window(tmp_path, settings):
    client, runtime, bridge, transport = whatsapp(tmp_path, settings)
    bridge.flush()
    assert transport.sent == []
    assert runtime.state()["messages"][0]["delivery"] == "awaiting_session"
    # A real inbound opens the session, then the full read-back is sent.
    assert post_signed(client, "HI", sid="SMhi").status_code == 200
    assert "Austin Energy" in transport.sent[0][1]
    ref = next(iter(runtime.state()["requests"]))
    response = post_signed(client, f"YES {ref}", sid="SMconfirm")
    assert response.status_code == 200 and b"<Response" in response.data
    assert runtime.state()["requests"][ref]["status"] == "scheduled"
    runtime.advance(2)
    bridge.flush()
    assert any("Paid $120.00" in body for _, body in transport.sent)


def test_whatsapp_rejects_unsigned_and_unconfigured_senders_and_ui_consent(tmp_path, settings):
    client, runtime, bridge, transport = whatsapp(tmp_path, settings)
    ref = next(iter(runtime.state()["requests"]))
    assert post_signed(client, f"YES {ref}", signature="forged").status_code == 403
    assert post_signed(client, f"YES {ref}", sender="whatsapp:+15559999999").status_code == 200
    assert runtime.state()["requests"][ref]["user_approved"] is False
    assert transport.sent == []
    assert client.post("/api/message", headers=AUTH, json={"actor":"senior","text":"Yes","request_id":ref}).status_code == 409


def test_code_without_delivered_readback_cannot_authorize_over_whatsapp(tmp_path, settings):
    client, runtime, _, transport = whatsapp(tmp_path, settings)
    ref = next(iter(runtime.state()["requests"]))
    # Even the real sender must receive the complete read-back before agreeing.
    post_signed(client, f"YES {ref}", sid="SMpremature")
    assert runtime.state()["requests"][ref]["user_approved"] is False
    assert "Austin Energy" in transport.sent[0][1]  # queued read-back delivered after premature reply


def test_twilio_sid_replay_survives_restart_and_reset(tmp_path, settings):
    client, runtime, _, _ = whatsapp(tmp_path, settings)
    post_signed(client, "HI", sid="SMhi")
    ref = next(iter(runtime.state()["requests"]))
    post_signed(client, f"YES {ref}", sid="SMonce")
    runtime.reset()
    fresh = next(iter(runtime.state()["requests"]))
    post_signed(client, f"YES {fresh}", sid="SMonce")
    assert runtime.state()["requests"][fresh]["user_approved"] is False
    restarted = AutopayDemo(runtime.database, external=True)
    assert not restarted.receive("senior", f"YES {fresh}", "SMonce", source="twilio")


def test_wakeup_template_only_once_and_never_counts_as_consent(tmp_path, settings):
    _, runtime, bridge, transport = whatsapp(tmp_path, settings, wake=True)
    templates = []
    transport.send_template = lambda to: templates.append(to)
    bridge.flush()
    bridge.flush()
    assert templates == [MOM]
    assert transport.sent == []
    req = next(iter(runtime.state()["requests"].values()))
    assert req["status"] == "awaiting_user" and req["delivered_roles"] == []


def test_uncertain_notification_failure_is_visible_without_retry_spam(tmp_path, settings):
    _, runtime, bridge, transport = whatsapp(tmp_path, settings)
    transport.record_inbound(MOM)
    def fail(*args):
        raise RuntimeError("secret-should-not-appear")
    transport.send_reply = fail
    bridge.flush()
    bridge.flush()
    message = runtime.state()["messages"][0]
    assert message["delivery"] == "failed" and message["delivery_detail"] == "RuntimeError"
    assert "secret-should-not-appear" not in str(runtime.state())


def test_expired_proposal_is_not_sent_when_session_finally_opens(tmp_path, settings):
    client, runtime, bridge, transport = whatsapp(tmp_path, settings)
    bridge.flush()
    runtime.advance(2)
    post_signed(client, "HI", sid="SMlate")
    assert not any("May I schedule" in body for _, body in transport.sent)
    assert runtime.state()["messages"][0]["delivery"] == "superseded"


def test_fresh_inbound_can_retry_a_previously_failed_readback(tmp_path, settings):
    client, runtime, bridge, transport = whatsapp(tmp_path, settings)
    transport.record_inbound(MOM)
    original = transport.send_reply
    transport.send_reply = lambda *args: (_ for _ in ()).throw(RuntimeError("uncertain send"))
    bridge.flush()
    assert runtime.state()["messages"][0]["delivery"] == "failed"
    transport.send_reply = original
    post_signed(client, "HI", sid="SMretry")
    assert any("May I schedule" in body for _, body in transport.sent)


def test_signature_uses_configured_public_url(tmp_path, settings):
    from dataclasses import replace
    settings = replace(settings, public_base_url="https://demo.example.test")
    client, _, _, _ = whatsapp(tmp_path, settings)
    params = {"From":MOM, "Body":"HI", "MessageSid":"SMpublic"}
    sig = RequestValidator(DUMMY_TOKEN).compute_signature("https://demo.example.test/whatsapp/inbound", params)
    assert client.post("/whatsapp/inbound", data=params, headers={"X-Twilio-Signature":sig}).status_code == 200


def test_bridge_roles_and_blank_family_never_default_to_senior(tmp_path, settings):
    _, runtime, bridge, transport = whatsapp(tmp_path, settings, family="")
    with runtime._transaction() as (_, state):
        runtime._emit(state, "family", "A flagged payment needs review")
    bridge.flush()
    assert bridge.actor_for(MOM) == "senior"
    assert bridge.actor_for("") is None
    assert runtime.state()["messages"][-1]["delivery"] == "unconfigured"
    assert transport.sent == []


def test_mode_cannot_silently_switch_a_persisted_bank(tmp_path):
    path = tmp_path / "isolated.sqlite3"
    AutopayDemo(path)
    with pytest.raises(ValueError, match="separate"):
        AutopayDemo(path, external=True)


def test_whatsapp_settings_do_not_need_openai_or_nessie_keys(monkeypatch):
    from backend.demo import whatsapp as module
    monkeypatch.setattr(module, "load_dotenv", lambda *a, **kw: None)
    for name, value in {"TWILIO_ACCOUNT_SID":"ACdummy", "TWILIO_AUTH_TOKEN":"dummy", "TWILIO_WHATSAPP_FROM":"whatsapp:+15550002222", "MOM_WHATSAPP_TO":MOM, "PUBLIC_BASE_URL":"https://demo.example.test"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("NESSIE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings, family, wake = demo_twilio_settings()
    assert settings.public_base_url == "https://demo.example.test"
    monkeypatch.setenv("STEADY_TRUSTED_WHATSAPP_TO", MOM)
    with pytest.raises(Exception, match="different"):
        demo_twilio_settings()
