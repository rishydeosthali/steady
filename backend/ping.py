"""Send the approved content template to Mom so she can open the thread.

    python -m backend.ping

Use this when there has been no recent inbound message. Free-form messages are
not allowed outside the 24 hour WhatsApp session; the template is.
"""

from __future__ import annotations

import logging
import sys

from .config import ConfigError, load_settings
from .twilio_client import TemplateUnavailable, TwilioWhatsApp
from twilio.base.exceptions import TwilioRestException


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Stopping: {exc}", file=sys.stderr)
        return 1
    try:
        sid = TwilioWhatsApp(settings).send_template(variables=settings.content_variables or None)
    except TemplateUnavailable as exc:
        print(f"Not sent: {exc}", file=sys.stderr)
        return 1
    except TwilioRestException as exc:
        print(f"Twilio rejected the send: HTTP {exc.status}, code {exc.code}", file=sys.stderr)
        return 1
    print(f"Template sent to {settings.mom_whatsapp_to}. Message SID: {sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
