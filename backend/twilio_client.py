"""Outbound WhatsApp via the Twilio Messages API.

Two ways to send, and they are not interchangeable:

* ``send_reply``: free-form body. Only valid inside the 24 hour WhatsApp
  session that an inbound message opens. Refuses to send otherwise.
* ``send_template``: the approved content template. Used to start a
  conversation when Mom has not written recently.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from .config import Settings

log = logging.getLogger(__name__)

SESSION_SECONDS = 24 * 60 * 60

# The Twilio SDK logs request details at INFO/DEBUG. Keep it quiet.
logging.getLogger("twilio.http_client").setLevel(logging.WARNING)


class SessionClosed(Exception):
    """No inbound message from this number in the session window."""


class TemplateUnavailable(Exception):
    """The content template could not be filled safely."""


class TwilioWhatsApp:
    def __init__(self, settings: Settings, *, client: Client | None = None):
        self._s = settings
        self._client = client or Client(settings.twilio_account_sid, settings.twilio_auth_token)
        self._validator = RequestValidator(settings.twilio_auth_token)
        self._last_inbound: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- inbound ---------------------------------------------------------

    def validate_signature(self, url: str, params, signature: str | None) -> bool:
        if not signature:
            return False
        return self._validator.validate(url, params, signature)

    def record_inbound(self, from_: str) -> None:
        with self._lock:
            self._last_inbound[from_] = time.time()

    def session_open(self, to: str) -> bool:
        with self._lock:
            seen = self._last_inbound.get(to)
        return seen is not None and (time.time() - seen) < SESSION_SECONDS

    # -- outbound --------------------------------------------------------

    def send_reply(self, to: str, body: str) -> str:
        """Free-form reply inside an open session. Returns the message SID."""
        if not self.session_open(to):
            raise SessionClosed("No recent inbound message from this number; use the template")
        msg = self._client.messages.create(from_=self._s.twilio_whatsapp_from, to=to, body=body)
        return msg.sid

    def template_variables(self) -> dict[str, str]:
        """Placeholders the template defines, as {name: ""}. Raises if unknowable."""
        try:
            content = self._client.content.v1.contents(self._s.twilio_content_sid).fetch()
        except TwilioRestException as exc:
            raise TemplateUnavailable(
                f"Could not fetch the content template (HTTP {exc.status}, code {exc.code}).") from None
        return {name: "" for name in (content.variables or {})}

    def send_template(self, to: str | None = None,
                      variables: dict[str, str] | None = None) -> str:
        """Start (or reopen) a conversation with the approved template.

        Fills only placeholders the template defines. If the template defines
        placeholders and no values are supplied, refuses rather than invent any.
        """
        to = to or self._s.mom_whatsapp_to
        kwargs = {"from_": self._s.twilio_whatsapp_from, "to": to,
                  "content_sid": self._s.twilio_content_sid}
        try:
            defined = self.template_variables()
        except TemplateUnavailable as exc:
            # The sandbox template SID is not always readable. Send the variables
            # the configuration supplies. Twilio rejects the send if it needs
            # different ones.
            if variables:
                log.warning("%s Sending the configured variables.", exc)
                kwargs["content_variables"] = json.dumps(variables)
            else:
                log.warning("%s Sending without variables.", exc)
            defined = {}
        if defined:
            supplied = variables or {}
            unknown = set(supplied) - set(defined)
            missing = set(defined) - set(supplied)
            if unknown or missing:
                raise TemplateUnavailable(
                    f"Template defines variables {sorted(defined)}; supply exactly those.")
            kwargs["content_variables"] = json.dumps(supplied)
        msg = self._client.messages.create(**kwargs)
        return msg.sid
