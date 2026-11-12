"""Production entry point: one Waitress process and one simulation scheduler.

Render starts this module automatically. Operators use the website only.
"""
from __future__ import annotations

import logging
import os


def main():
    from waitress import serve
    from .app import create_app
    from .config import load_settings, ConfigError
    from .twilio_client import TwilioWhatsApp, SessionClosed
    from .agent.audit import IdleWorker
    from .autonomy.controller import GoalWorker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("urllib3", "httpx", "httpcore", "openai", "twilio.http_client"):
        logging.getLogger(name).setLevel(logging.WARNING)
    settings = load_settings()
    if os.environ.get("STEADY_SIMULATION", "").strip() != "1":
        raise ConfigError("The hosted scenario studio requires STEADY_SIMULATION=1")
    app = create_app(settings)
    runtime = app.extensions["simulation"]
    # Real-time WhatsApp windows are independent of the paused simulation date.
    messenger = runtime.twilio

    def offer(text):
        with runtime.lock:
            try:
                messenger.send_reply(settings.mom_whatsapp_to, text)
            except SessionClosed:
                pass

    idle = IdleWorker(runtime.audit, offer)
    goals = GoalWorker(app.extensions["autonomy"]) if "autonomy" in app.extensions else None
    runtime.start()
    idle.start()
    if goals:
        goals.start()
    try:
        serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")),
              threads=8, clear_untrusted_proxy_headers=False)
    finally:
        runtime.stop()
        idle.stop()
        if goals:
            goals.stop()


if __name__ == "__main__":
    main()
