"""Slack DM transport. No WhatsApp session window or outbound SMTP is needed."""

import hashlib
import hmac
import threading
import time

import requests

from .payments.caregiver import MailError


class SlackError(RuntimeError):
    pass


class SlackMessenger:
    def __init__(self, settings):
        self.settings = settings
        self._channels = {}
        self._lock = threading.RLock()

    def api(self, method, payload):
        # Callers supply fixed method names, never response_url or user-provided URLs.
        try:
            with requests.post("https://slack.com/api/" + method, json=payload,
                               headers={"Authorization": "Bearer " + self.settings.slack_bot_token},
                               timeout=15, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise SlackError(f"Slack HTTP {response.status_code}")
                result = response.json()
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise SlackError("Slack rejected the request")
                return result
        except (requests.RequestException, ValueError) as exc:
            raise SlackError("Slack request failed: " + type(exc).__name__) from None

    def dm(self, user):
        if user not in {self.settings.slack_senior_user_id, self.settings.slack_caregiver_user_id} or not user:
            raise SlackError("Unconfigured Slack recipient")
        with self._lock:
            if user not in self._channels:
                channel = self.api("conversations.open", {"users": user}).get("channel", {}).get("id")
                if not isinstance(channel, str) or not channel.startswith("D"):
                    raise SlackError("Slack did not return a DM channel")
                self._channels[user] = channel
            return self._channels[user]

    def send_reply(self, to, text, *, blocks=None):
        payload = {"channel": self.dm(to), "text": text, "mrkdwn": False,
                   "unfurl_links": False, "unfurl_media": False}
        if blocks:
            payload["blocks"] = blocks
        result = self.api("chat.postMessage", payload)
        if not isinstance(result.get("ts"), str):
            raise SlackError("Slack response missing message timestamp")
        return result["ts"]

    def session_open(self, to):
        return bool(to and to == self.settings.slack_senior_user_id)

    def record_inbound(self, from_):
        pass

    def validate_signature(self, *args):
        # Twilio webhooks are disabled when this transport is selected.
        return False

    def fetch_media(self, *args):
        raise SlackError("Slack bill-photo intake is not supported; type the bill details")

    def signed(self, raw, timestamp, signature):
        if not self.settings.slack_signing_secret or not timestamp or not signature:
            return False
        try:
            if abs(time.time() - int(timestamp)) > 300:
                return False
        except (ValueError, TypeError):
            return False
        expected = "v0=" + hmac.new(self.settings.slack_signing_secret.encode(),
                                    b"v0:" + timestamp.encode() + b":" + raw,
                                    hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


class SlackCaregiver:
    delivery_name = "Slack"

    def __init__(self, messenger):
        self.messenger = messenger
        self.user = messenger.settings.slack_caregiver_user_id

    def request_approval(self, payment, token):
        text = (f"Steady demo approval: {payment['payee']}, {payment['amount']}, {payment['date']}. "
                "The senior agreed. Nothing is paid without your approval. All money is simulated.")
        blocks = [{"type": "section", "text": {"type": "plain_text", "text": text}},
                  {"type": "actions", "elements": [
                      {"type": "button", "text": {"type": "plain_text", "text": label},
                       "action_id": "steady_" + decision, "value": token}
                      for decision, label in (("approve", "Approve"), ("deny", "Deny"))]}]
        try:
            self.messenger.send_reply(self.user, text, blocks=blocks)
        except SlackError as exc:
            raise MailError(str(exc)) from None

    def notify(self, reason, details, urgency="soon"):
        safe = ", ".join(f"{key}: {str(details[key])[:120]}" for key in
                         ("payee", "merchant", "amount", "date", "kind", "what_she_said") if details.get(key))
        try:
            self.messenger.send_reply(self.user, f"Steady demo alert: {reason[:80]}. {safe}")
        except SlackError as exc:
            raise MailError(str(exc)) from None
