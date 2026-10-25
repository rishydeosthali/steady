"""Run the synthetic bill autopayment demo, optionally connected to real WhatsApp."""

import argparse
import logging
import os
import secrets
from datetime import date
from pathlib import Path

from backend.config import ConfigError
from backend.demo.autopay import AutopayDemo, default_snapshot
from backend.demo.data import load_scenario
from backend.demo.nessie import FixtureRequestHandler
from backend.demo.runner import DemoWorker
from backend.demo.webapp import create_autopay_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5010)
    parser.add_argument("--db", type=Path, default=Path("work/autopay-demo.sqlite3"))
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--scenario", help="Use one of demo_data list's scenarios instead of the three-day example")
    parser.add_argument("--whatsapp", action="store_true", help="Enable real messaging using .env Twilio settings")
    parser.add_argument("--seconds-per-day", type=int, default=0, help="0 pauses dates; 5..120 advances automatically")
    args = parser.parse_args()
    settings, family, wake = None, "", False
    if args.whatsapp:
        from backend.demo.whatsapp import demo_twilio_settings
        try:
            settings, family, wake = demo_twilio_settings()
        except ConfigError as exc:
            parser.error(str(exc))
    # A token can be provided in the process environment in either mode.
    token = os.environ.get("STEADY_DEMO_TOKEN") or secrets.token_urlsafe(24)
    try:
        snapshot = load_scenario(args.scenario, as_of=args.as_of) if args.scenario else default_snapshot(args.as_of)
        runtime = AutopayDemo(args.db, snapshot, external=args.whatsapp)
    except ValueError as exc:
        parser.error(str(exc))
    bridge = None
    if args.whatsapp:
        from backend.demo.whatsapp import WhatsAppBridge
        bridge = WhatsAppBridge(runtime, settings, family, allow_wakeup=wake)
    worker = DemoWorker(runtime, bridge)
    try:
        worker.set_rate(args.seconds_per_day)
        app = create_autopay_app(runtime, token, bridge=bridge, worker=worker)
    except ValueError as exc:
        parser.error(str(exc))
    runtime.tick()
    print(f"\nSteady autopayment demo: http://127.0.0.1:{args.port}")
    print(f"Demo access token: {token}")
    print("Bank: SYNTHETIC LOCAL LEDGER. No real Nessie writes or utility payments.")
    print("Channel: " + ("REAL WHATSAPP (Twilio charges may apply)." if bridge else "LOCAL PHONE SIMULATOR."))
    print("Data and schedules persist in the selected SQLite file. Reset in the UI, or select a new --db for a new scenario.")
    if bridge:
        print("Use the exact HTTPS tunnel origin in PUBLIC_BASE_URL and route /whatsapp/inbound to this port.")
        print("Without STEADY_WAKE_TEMPLATE_SID, text HI from the phone first to open its session.")
    logging.basicConfig(level=logging.INFO)
    worker.start()
    try:
        app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False,
                request_handler=FixtureRequestHandler, threaded=True)
    finally:
        worker.stop()


if __name__ == "__main__":
    main()
