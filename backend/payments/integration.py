"""Opt-in integration for Rishy's original `python -m backend.app` entry point."""

import hmac
import logging
import os
import threading
from dataclasses import replace
from datetime import date

from flask import jsonify, request

from ..config import ConfigError
from ..twilio_client import TwilioWhatsApp
from .client import BillerClient
from .service import PaymentService

log = logging.getLogger(__name__)


def build_payments(settings, nessie, twilio):
    required = ("BILLER_BASE_URL","BILLER_API_TOKEN","STEADY_PAYMENT_CONTROL_TOKEN")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigError("External biller mode needs: " + ", ".join(missing))
    token = os.environ["STEADY_PAYMENT_CONTROL_TOKEN"].strip()
    if len(token) < 16:
        raise ConfigError("STEADY_PAYMENT_CONTROL_TOKEN must be at least 16 characters")
    try:
        biller = BillerClient(os.environ["BILLER_BASE_URL"].strip(),os.environ["BILLER_API_TOKEN"].strip())
        frozen = os.environ.get("STEADY_BILLER_DEMO_DATE", "").strip()
        demo_date = date.fromisoformat(frozen) if frozen else None
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    wakeup = None
    wake_sid = os.environ.get("BILLER_WAKE_TEMPLATE_SID", "").strip()
    if wake_sid:
        wake_transport = TwilioWhatsApp(replace(settings,twilio_content_sid=wake_sid))
        wakeup = lambda: wake_transport.send_template(to=settings.mom_whatsapp_to)
    service = PaymentService(os.environ.get("STEADY_PAYMENT_DB", "work/external-payments.sqlite3"),
                             biller,nessie,twilio,settings.mom_whatsapp_to,
                             customer_name=settings.customer_first_name,
                             customer_ref=os.environ.get("BILLER_CUSTOMER_REF","demo-margaret"),
                             demo_date=demo_date,wakeup=wakeup)
    return service,token


def register_controls(app, service, token):
    def authorized():
        return hmac.compare_digest(request.headers.get("Authorization", "").encode(),("Bearer "+token).encode())

    @app.get("/demo/payments",endpoint="external_payment_status")
    def status():
        if not authorized():
            return jsonify(error="Unauthorized"),401
        return jsonify(service.state())

    @app.post("/demo/payments/check",endpoint="external_payment_check")
    def check():
        if not authorized():
            return jsonify(error="Unauthorized"),401
        service.tick()
        service.flush()
        return jsonify(service.state())

    @app.post("/demo/payments/advance",endpoint="external_payment_advance")
    def advance():
        if not authorized():
            return jsonify(error="Unauthorized"),401
        body = request.get_json(silent=True)
        if not isinstance(body,dict):
            return jsonify(error="Send a JSON object with days"),400
        try:
            service.advance(body.get("days",1))
        except ValueError as exc:
            return jsonify(error=str(exc)),400
        service.flush()
        return jsonify(service.state())

    @app.after_request
    def protect_payment_state(response):
        if request.path.startswith("/demo/payments"):
            response.headers["Cache-Control"] = "no-store"
        return response


class PaymentWorker:
    def __init__(self, service, *, interval=10):
        self.service,self.interval = service,interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run,name="external-biller-monitor",daemon=True)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.service.tick()
                self.service.flush()
            except Exception as exc:
                log.error("External biller worker failed: %s",type(exc).__name__)
            self.stop_event.wait(self.interval)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)
