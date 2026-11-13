"""HTTPS caregiver delivery: no real emails or provider calls."""

import pytest
import requests

from backend.config import ConfigError
from backend.payments.caregiver import CaregiverMailer, MailError
from backend.payments.integration import build_mailer
from backend.payments.resend_mailer import ResendMailer


PAYMENT = {"payee": "Austin Energy", "amount": "$120.00", "date": "Thursday, Nov 12"}
TOKEN = "approval-credential"


class Reply:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = {"id": "email-id"} if body is None else body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def mailer():
    return ResendMailer("dummy-api-key", "onboarding@resend.dev", "caregiver@example.com",
                        base_url="https://steady.example")


def test_approval_uses_https_and_preserves_private_content_with_stable_retry_key(monkeypatch):
    sent = []

    def post(url, **kwargs):
        sent.append((url, kwargs))
        return Reply()

    monkeypatch.setattr(requests, "post", post)
    sender = mailer()
    sender.request_approval(PAYMENT, TOKEN)
    sender.request_approval(PAYMENT, TOKEN)
    assert sent[0] == sent[1]
    url, options = sent[0]
    assert url == "https://api.resend.com/emails"
    assert options["timeout"] == 15 and options["allow_redirects"] is False
    assert options["headers"]["Authorization"] == "Bearer dummy-api-key"
    assert TOKEN not in options["headers"]["Idempotency-Key"]
    payload = options["json"]
    assert payload["to"] == ["caregiver@example.com"]
    assert payload["from"] == "Steady <onboarding@resend.dev>"
    assert "Austin Energy" in payload["subject"]
    for field in ("html", "text"):
        assert "$120.00" in payload[field]
        assert f"https://steady.example/caregiver/{TOKEN}?decision=approve" in payload[field]
        assert "balance" not in payload[field]
    sender.request_approval(PAYMENT, "new-token")
    assert sent[-1][1]["headers"]["Idempotency-Key"] != options["headers"]["Idempotency-Key"]
    assert "dummy-api-key" not in repr(sender)


def test_alert_also_uses_https_and_omits_financial_secrets(monkeypatch):
    sent = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: sent.append(kw) or Reply())
    mailer().notify("duplicate charge", {"merchant": "Dentist", "amount": "$120",
                                        "balance": "$9999"}, "now")
    assert "Dentist" in sent[0]["json"]["text"]
    assert "$9999" not in str(sent[0])
    assert "Idempotency-Key" not in sent[0]["headers"]


@pytest.mark.parametrize("response", [Reply(401), Reply(403), Reply(429), Reply(500),
                                       Reply(302), Reply(200, {}), Reply(200, []),
                                       Reply(200, {"id": None}),
                                       Reply(200, ValueError("private response"))])
def test_provider_failure_stays_an_email_failure_without_leaking_details(monkeypatch, response):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: response)
    with pytest.raises(MailError) as error:
        mailer().request_approval(PAYMENT, TOKEN)
    assert "Caregiver email failed" in str(error.value)
    for secret in ("private response", "dummy-api-key", "caregiver@example.com", TOKEN):
        assert secret not in str(error.value)


def test_timeout_can_be_retried_with_the_same_approval_key(monkeypatch):
    calls = []

    def post(*args, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise requests.Timeout("secret provider details")
        return Reply()

    monkeypatch.setattr(requests, "post", post)
    sender = mailer()
    with pytest.raises(MailError, match="Resend Timeout") as error:
        sender.request_approval(PAYMENT, TOKEN)
    assert "secret" not in str(error.value)
    sender.request_approval(PAYMENT, TOKEN)
    assert calls[0] == calls[1]


def test_resend_selection_does_not_require_gmail_credentials(monkeypatch, settings):
    monkeypatch.setenv("RESEND_API_KEY", "dummy-api-key")
    monkeypatch.setenv("CAREGIVER_EMAIL", "caregiver@example.com")
    for key in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "RESEND_FROM", "STEADY_APPROVAL_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    sender = build_mailer(settings)
    assert isinstance(sender, ResendMailer)
    assert sender.sender == "onboarding@resend.dev"
    monkeypatch.setenv("RESEND_FROM", "steady@verified.example")
    assert build_mailer(settings).sender == "steady@verified.example"
    monkeypatch.delenv("CAREGIVER_EMAIL")
    with pytest.raises(ConfigError, match="CAREGIVER_EMAIL"):
        build_mailer(settings)


def test_gmail_remains_available_when_resend_key_is_absent(monkeypatch, settings):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("GMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "dummy-password")
    monkeypatch.setenv("CAREGIVER_EMAIL", "caregiver@example.com")
    assert type(build_mailer(settings)) is CaregiverMailer
