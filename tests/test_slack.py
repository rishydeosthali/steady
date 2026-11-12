import hashlib
import hmac
import json
import time
from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest
from flask import Flask

from backend.slack_client import SlackMessenger, SlackCaregiver, SlackError
from backend.slack_routes import register_slack
from backend.config import ConfigError, load_settings
from tests.test_app import InlineExecutor


@pytest.fixture
def slack_settings(settings):
    return replace(settings, channel="slack", slack_bot_token="dummy-token",
                   slack_signing_secret="dummy-secret", slack_team_id="Tdemo",
                   slack_senior_user_id="Usenior", slack_caregiver_user_id="Ucaregiver")


def signed(client, path, payload, secret="dummy-secret", stamp=None, form=False):
    from urllib.parse import urlencode
    raw = urlencode({"payload":json.dumps(payload)}) if form else json.dumps(payload)
    stamp = stamp or str(int(time.time()))
    signature = "v0=" + hmac.new(secret.encode(), f"v0:{stamp}:{raw}".encode(), hashlib.sha256).hexdigest()
    return client.post(path, data=raw, content_type="application/x-www-form-urlencoded" if form else "application/json",
                       headers={"X-Slack-Signature":signature,"X-Slack-Request-Timestamp":stamp})


def setup_routes(tmp_path, settings, replies, decisions):
    messenger = SlackMessenger(settings)
    messenger.send_reply = lambda *args: replies.append(args)
    payments = SimpleNamespace(caregiver_decide=lambda *args: decisions.append(args) or
                              ({"status_text":"Approved"},None))
    runtime = SimpleNamespace(directory=tmp_path,lock=threading.RLock(),payments=payments,tick=lambda:None)
    app = Flask(__name__)
    register_slack(app, settings, messenger, InlineExecutor(), lambda *args: replies.append(args), runtime)
    return app.test_client()


def test_signed_event_challenge_and_durable_deduplication(tmp_path, slack_settings):
    replies, decisions = [], []
    client = setup_routes(tmp_path,slack_settings,replies,decisions)
    assert signed(client,"/slack/events",{"type":"url_verification","challenge":"abc"}).json == {"challenge":"abc"}
    event = {"type":"event_callback","team_id":"Tdemo","event_id":"Ev1",
             "event":{"type":"message","channel_type":"im","user":"Usenior","text":"hi"}}
    assert signed(client,"/slack/events",event).status_code == 200
    assert replies == [("Usenior","slack:Ev1","hi")]
    client = setup_routes(tmp_path,slack_settings,replies,decisions)
    assert signed(client,"/slack/events",event).status_code == 200
    assert len(replies) == 1
    for change in ({"user":"Uoutsider"},{"bot_id":"Bbot"},{"channel_type":"channel"},{"subtype":"message_changed"}):
        event["event"].update(change)
        signed(client,"/slack/events",event)
    assert len(replies) == 1
    event["team_id"] = "Tother"
    assert signed(client,"/slack/events",event).status_code == 403
    assert signed(client,"/slack/events",event,secret="wrong").status_code == 403
    assert signed(client,"/slack/events",event,stamp="1").status_code == 403


def test_only_separate_caregiver_can_approve(tmp_path,slack_settings):
    replies, decisions = [], []
    client = setup_routes(tmp_path,slack_settings,replies,decisions)
    action = {"type":"block_actions","team":{"id":"Tdemo"},"user":{"id":"Usenior"},
              "actions":[{"action_id":"steady_approve","value":"t"*43}]}
    assert signed(client,"/slack/actions",action,form=True).status_code == 403
    action["user"]["id"] = "Ucaregiver"
    assert signed(client,"/slack/actions",action,form=True).status_code == 200
    assert decisions == [("t"*43,"approve")]
    action["team"] = None
    assert signed(client,"/slack/actions",action,form=True).status_code == 400


def test_dm_transport_and_private_caregiver_buttons(slack_settings):
    messenger = SlackMessenger(slack_settings)
    calls = []
    def api(method,payload):
        calls.append((method,payload))
        return {"channel":{"id":"Ddemo"}} if method == "conversations.open" else {"ts":"123.45"}
    messenger.api = api
    assert messenger.send_reply("Usenior","hi") == "123.45"
    assert messenger.session_open("Usenior")
    assert not messenger.session_open("Uother")
    with pytest.raises(SlackError):
        messenger.send_reply("Uother","secret")
    SlackCaregiver(messenger).request_approval({"payee":"Utility","amount":"$120","date":"today"},"t"*43)
    body = calls[-1][1]
    assert "balance" not in json.dumps(body)
    assert [b["action_id"] for b in body["blocks"][1]["elements"]] == ["steady_approve","steady_deny"]


def test_slack_settings_without_twilio(monkeypatch,tmp_path):
    monkeypatch.setattr("backend.config.load_dotenv",lambda *a,**k:None)
    for key,value in {"STEADY_SIMULATION":"1","STEADY_CHANNEL":"slack","OPENAI_API_KEY":"dummy",
                      "SLACK_BOT_TOKEN":"dummy","SLACK_SIGNING_SECRET":"dummy","SLACK_TEAM_ID":"Tdemo",
                      "SLACK_SENIOR_USER_ID":"Usenior","SLACK_CAREGIVER_USER_ID":""}.items():
        monkeypatch.setenv(key,value)
    for key in ("TWILIO_ACCOUNT_SID","TWILIO_AUTH_TOKEN","TWILIO_WHATSAPP_FROM","TWILIO_CONTENT_SID","MOM_WHATSAPP_TO"):
        monkeypatch.delenv(key,raising=False)
    assert load_settings().recipient == "Usenior"
    monkeypatch.setenv("SLACK_CAREGIVER_USER_ID","Usenior")
    with pytest.raises(ConfigError,match="different Slack member"):
        load_settings()
