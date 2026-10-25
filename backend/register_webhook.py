"""Point the WhatsApp sender's "When a message comes in" at this app.

    python -m backend.register_webhook https://<tunnel-host>

Prints the exact webhook URL. If the sender is an incoming phone number on the
account, sets ``sms_url`` through the Twilio API. If the API cannot update the
sender (WhatsApp sandbox senders have no API for this), prints the Console
path and the URL to paste. Nothing is claimed registered unless the API
accepted it.
"""

from __future__ import annotations

import sys
from urllib.parse import urlparse

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from .config import ConfigError, load_settings

CONSOLE_PATH = ("Twilio Console > Messaging > Try it out > Send a WhatsApp message > "
                "Sandbox settings")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].startswith("https://"):
        print("Usage: python -m backend.register_webhook https://<tunnel-host>", file=sys.stderr)
        return 2
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Stopping: {exc}", file=sys.stderr)
        return 1

    host = urlparse(argv[1]).netloc
    inbound = f"https://{host}/whatsapp/inbound"
    status = f"https://{host}/whatsapp/status"
    print(f"Webhook URL (method POST): {inbound}")
    print(f"Status callback URL (method POST): {status}")

    number = settings.twilio_whatsapp_from.removeprefix("whatsapp:")
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    try:
        matches = client.incoming_phone_numbers.list(phone_number=number, limit=1)
    except TwilioRestException as exc:
        print(f"Could not list incoming numbers (HTTP {exc.status}, code {exc.code}).")
        matches = []

    if matches:
        matches[0].update(sms_url=inbound, sms_method="POST")
        print(f"Set sms_url on {number} through the Twilio API. Registered.")
        return 0

    print(f"\n{number} is not an incoming phone number on this account, so the API cannot "
          "set its webhook.\nSet it by hand:")
    print(f"  1. Open: {CONSOLE_PATH}")
    print(f"  2. 'When a message comes in': {inbound}   Method: POST")
    print(f"  3. 'Status callback URL': {status}   Method: POST")
    print("  4. Save.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
