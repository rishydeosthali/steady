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
from pathlib import Path

from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from .config import REPO_ROOT, Settings

log = logging.getLogger(__name__)

SESSION_SECONDS = 24 * 60 * 60
SESSION_PATH = REPO_ROOT / "data" / "sessions.json"

# The Twilio SDK logs request details at INFO/DEBUG. Keep it quiet.
logging.getLogger("twilio.http_client").setLevel(logging.WARNING)


class SessionClosed(Exception):
    """No inbound message from this number in the session window."""


class TemplateUnavailable(Exception):
    """The content template could not be filled safely."""


class TwilioWhatsApp:
    def __init__(self, settings: Settings, *, client: Client | None = None,
                 session_path: Path | None = SESSION_PATH):
        self._s = settings
        self._client = client or Client(settings.twilio_account_sid, settings.twilio_auth_token)
        self._validator = RequestValidator(settings.twilio_auth_token)
        self._session_path = Path(session_path) if session_path else None
        self._last_inbound: dict[str, float] = self._read_sessions()
        self._lock = threading.Lock()

    # -- inbound ---------------------------------------------------------

    def validate_signature(self, url: str, params, signature: str | None) -> bool:
        if not signature:
            return False
        return self._validator.validate(url, params, signature)

    def record_inbound(self, from_: str) -> None:
        with self._lock:
            self._last_inbound[from_] = time.time()
            self._write_sessions()

    def session_open(self, to: str) -> bool:
        now = time.time()
        with self._lock:
            seen = self._last_inbound.get(to)
            if seen is not None and (now - seen) < SESSION_SECONDS:
                return True
        seen = self._remember_from_twilio(to)
        if seen is None or (now - seen) >= SESSION_SECONDS:
            return False
        with self._lock:
            self._last_inbound[to] = seen
            self._write_sessions()
        return True

    def _read_sessions(self) -> dict[str, float]:
        if self._session_path is None or not self._session_path.exists():
            return {}
        try:
            raw = json.loads(self._session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in raw.items():
            try:
                out[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def _write_sessions(self) -> None:
        if self._session_path is None:
            return
        try:
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._session_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._last_inbound), encoding="utf-8")
            tmp.replace(self._session_path)
        except OSError:
            log.warning("could not persist WhatsApp session window")

    def _remember_from_twilio(self, from_: str) -> float | None:
        """Ask Twilio for the last inbound if this process forgot (restart)."""
        messages = getattr(self._client, "messages", None)
        if messages is None or not hasattr(messages, "list"):
            return None
        try:
            rows = messages.list(from_=from_, to=self._s.twilio_whatsapp_from, limit=1)
        except Exception as exc:  # noqa: BLE001 - transport or auth; never log the body
            log.warning("session lookup failed: %s", type(exc).__name__)
            return None
        if not rows:
            return None
        when = getattr(rows[0], "date_sent", None) or getattr(rows[0], "date_created", None)
        return when.timestamp() if when is not None and hasattr(when, "timestamp") else None

    # -- outbound --------------------------------------------------------

    def fetch_media(self, url: str) -> tuple[bytes, str]:
        """Download one inbound WhatsApp image. Host and size are checked in code."""
        from .agent.photo import fetch_twilio_media
        return fetch_twilio_media(url, self._s.twilio_account_sid, self._s.twilio_auth_token)

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
