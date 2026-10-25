"""Optional real WhatsApp transport around the explicitly simulated bank.

Nothing sends until the operator starts the runner with --whatsapp. Credentials
stay in .env. Only the two configured senders are allowed to make decisions.
"""

from __future__ import annotations

import os
import re
import threading
from urllib.parse import urlsplit

from dotenv import load_dotenv

from ..config import ConfigError, REPO_ROOT, Settings
from ..twilio_client import TwilioWhatsApp


def demo_twilio_settings() -> tuple[Settings, str, bool]:
    load_dotenv(REPO_ROOT / ".env", override=False)
    required = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "MOM_WHATSAPP_TO", "PUBLIC_BASE_URL")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigError("WhatsApp mode needs: " + ", ".join(missing))
    env = lambda name: os.environ.get(name, "").strip()
    public = urlsplit(env("PUBLIC_BASE_URL"))
    if public.scheme != "https" or not public.netloc or public.username or public.password or public.query or public.fragment or public.path not in {"", "/"}:
        raise ConfigError("PUBLIC_BASE_URL must be an HTTPS origin, without credentials or a path")
    family = env("STEADY_TRUSTED_WHATSAPP_TO")
    if family and family == env("MOM_WHATSAPP_TO"):
        raise ConfigError("Senior and trusted-person numbers must be different")
    for name in ("TWILIO_WHATSAPP_FROM", "MOM_WHATSAPP_TO", "STEADY_TRUSTED_WHATSAPP_TO"):
        value = env(name)
        if value and not re.fullmatch(r"whatsapp:\+[1-9][0-9]{6,14}", value):
            raise ConfigError(name + " must use whatsapp:+<international number>")
    wake_sid = env("STEADY_WAKE_TEMPLATE_SID")
    settings = Settings(
        twilio_account_sid=env("TWILIO_ACCOUNT_SID"), twilio_auth_token=env("TWILIO_AUTH_TOKEN"),
        twilio_whatsapp_from=env("TWILIO_WHATSAPP_FROM"), mom_whatsapp_to=env("MOM_WHATSAPP_TO"),
        twilio_content_sid=wake_sid, public_base_url=env("PUBLIC_BASE_URL").rstrip("/"),
        # This runner uses neither OpenAI nor a real Nessie key.
        nessie_api_key="unused-simulation", nessie_base_url="https://synthetic.nessie.invalid",
        openai_api_key="unused-simulation",
    )
    return settings, family, bool(wake_sid)


class WhatsAppBridge:
    def __init__(self, runtime, settings, family_to="", *, allow_wakeup=False, twilio=None):
        self.runtime, self.settings = runtime, settings
        self.twilio = twilio or TwilioWhatsApp(settings)
        self.addresses = {"senior": settings.mom_whatsapp_to, "family": family_to}
        self.allow_wakeup = allow_wakeup
        self._lock = threading.Lock()

    def actor_for(self, sender):
        return next((role for role, address in self.addresses.items() if address and address == sender), None)

    def flush(self):
        """Serialized outbox delivery; no free-form message outside its session.

        A send with an uncertain result is displayed as failed, not retried in
        a tight loop. Payment idempotency is independent of notification delivery.
        """
        with self._lock:
            snapshot = self.runtime.state()
            for message in snapshot["messages"]:
                if message["direction"] != "out" or message["delivery"] != "pending":
                    continue
                actor = message["actor"]
                req = snapshot["requests"].get(message.get("request_id"))
                if req and req["amount_cents"] is not None and req["payment_date"]:
                    is_prompt = message["body"] == self.runtime._prompt(req, family=actor == "family")
                    expected = "awaiting_user" if actor == "senior" else "awaiting_family"
                    if is_prompt and req["status"] != expected:
                        self.runtime.delivery(message["id"], "superseded")
                        continue
                to = self.addresses[actor]
                if not to:
                    self.runtime.delivery(message["id"], "unconfigured")
                    continue
                try:
                    if not self.twilio.session_open(to):
                        self.runtime.delivery(message["id"], "awaiting_session")
                        if self.allow_wakeup and self.runtime.claim_wakeup(actor):
                            # Must be an approved, no-variable, generic wake-up
                            # template. It never conveys approval or moves funds.
                            self.twilio.send_template(to=to)
                        continue
                    self.twilio.send_reply(to, message["body"])
                    self.runtime.delivery(message["id"], "sent")
                except Exception as exc:
                    self.runtime.delivery(message["id"], "failed", detail=type(exc).__name__)

    def inbound(self, actor, sender, body, sid):
        self.twilio.record_inbound(sender)
        self.runtime.resume_notifications(actor)
        self.runtime.receive(actor, body, sid, source="twilio")
        self.flush()
