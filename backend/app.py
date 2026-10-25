"""Flask app: the WhatsApp webhook.

Twilio waits about 15 seconds. Nessie plus a model call can miss that, so the
webhook validates the signature, answers with an empty TwiML ``<Response/>``
at once, and sends the real reply through the Messages API from a worker.
"""

from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, request
from twilio.twiml.messaging_response import MessagingResponse
from werkzeug.middleware.proxy_fix import ProxyFix

from .agent.llm import LLM
from .agent.loop import Agent
from .config import ConfigError, Settings, load_settings
from .nessie.client import NessieClient
from .twilio_client import SessionClosed, TwilioWhatsApp

log = logging.getLogger("steady")


def _mask(address: str) -> str:
    """'whatsapp:+17372353170' -> 'whatsapp:+*******3170' for logs."""
    prefix, _, number = address.partition(":")
    if not number:
        return "***"
    return f"{prefix}:{number[:1]}{'*' * max(len(number) - 5, 0)}{number[-4:]}"


def _empty_twiml() -> Response:
    return Response(str(MessagingResponse()), mimetype="text/xml")


def create_app(settings: Settings | None = None, *, agent: Agent | None = None,
               twilio: TwilioWhatsApp | None = None, executor: ThreadPoolExecutor | None = None,
               payments=None
               ) -> Flask:
    settings = settings or load_settings()
    twilio = twilio or TwilioWhatsApp(settings)
    control_token = None
    if payments is None and os.environ.get("STEADY_EXTERNAL_BILLER", "").strip() == "1":
        from .payments.integration import build_payments
        payments,control_token = build_payments(settings,
            NessieClient(settings.nessie_base_url,settings.nessie_api_key),twilio)
    if agent is None:
        agent = Agent(
            llm=LLM(settings.openai_api_key, settings.openai_model),
            nessie=NessieClient(settings.nessie_base_url, settings.nessie_api_key),
            customer_first_name=settings.customer_first_name,
            payments=payments,
        )
    executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="steady")
    seen_sids: set[str] = set()

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    if payments is not None:
        app.extensions["external_payments"] = payments
        if control_token:
            from .payments.integration import register_controls
            register_controls(app,payments,control_token)
    # Behind a tunnel the public URL differs from the local one. Twilio signs the
    # public URL, so rebuild it from the forwarded headers.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    def request_url() -> str:
        if settings.public_base_url:
            query = request.query_string.decode()
            return f"{settings.public_base_url}{request.path}" + (f"?{query}" if query else "")
        return request.url

    def signature_ok() -> bool:
        return twilio.validate_signature(request_url(), request.form,
                                         request.headers.get("X-Twilio-Signature"))

    def reply_to(from_: str, sid: str, body: str) -> None:
        try:
            texts = agent.respond(body,message_id=sid) if payments is not None else agent.respond(body)
            for text in texts:
                twilio.send_reply(from_, text)
            if payments is not None:
                payments.tick()
                payments.flush()
            log.info("replied to %s for %s", _mask(from_), sid)
        except SessionClosed:
            log.error("refused free-form send: no open session for %s", _mask(from_))
        except Exception as exc:  # noqa: BLE001 - never let a worker die silently
            # Type and code only: exception text can carry request details.
            log.error("reply failed for %s: %s code=%s", sid, type(exc).__name__,
                      getattr(exc, "code", None))

    @app.get("/healthz")
    def healthz() -> Response:
        return Response("ok", mimetype="text/plain")

    @app.post("/whatsapp/inbound")
    def inbound() -> Response:
        # Signature first. Nothing from the form is read before this passes.
        if not signature_ok():
            log.warning("rejected inbound request: bad signature")
            return Response("Forbidden", status=403)

        from_ = request.form.get("From", "")
        body = request.form.get("Body", "")
        sid = request.form.get("MessageSid", "")

        if from_ != settings.mom_whatsapp_to and (payments is not None or not settings.allow_any_sender):
            log.info("ignored message from %s", _mask(from_))
            return _empty_twiml()
        if payments is not None and (not sid or len(sid)>128):
            return Response("MessageSid required for payment decisions",status=400)
        if sid and sid in seen_sids:
            log.info("ignored duplicate delivery %s", sid)
            return _empty_twiml()
        if sid:
            seen_sids.add(sid)

        log.info("inbound %s from %s (%d chars)", sid, _mask(from_), len(body))
        twilio.record_inbound(from_)          # opens the free-form session
        executor.submit(reply_to, from_, sid, body)
        return _empty_twiml()

    @app.post("/whatsapp/status")
    def status() -> Response:
        if not signature_ok():
            return Response("Forbidden", status=403)
        return Response(status=204)           # never replies

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Libraries that log URLs (Nessie's key rides in the query string).
    for noisy in ("urllib3", "httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Stopping: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    try:
        app = create_app(settings)
    except ConfigError as exc:
        print(f"Stopping: {exc}",file=sys.stderr)
        raise SystemExit(1) from None
    worker = None
    if "external_payments" in app.extensions:
        from .payments.integration import PaymentWorker
        worker = PaymentWorker(app.extensions["external_payments"])
        worker.start()
    log.info("Steady WhatsApp agent listening on :5000 (webhook path /whatsapp/inbound)")
    try:
        app.run(host="127.0.0.1", port=5000, threaded=True)
    finally:
        if worker:
            worker.stop()


if __name__ == "__main__":
    main()
