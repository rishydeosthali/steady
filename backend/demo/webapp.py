"""Demo controls require a bearer token; external inbound requires Twilio's signature."""

from __future__ import annotations

import hmac
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, jsonify, render_template, request


def create_autopay_app(runtime, token: str, *, bridge=None, worker=None, executor=None):
    if not isinstance(token, str) or len(token) < 16:
        raise ValueError("Use a demo access token of at least 16 characters")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="demo-webhook")

    @app.before_request
    def authorize_controls():
        if request.path.startswith("/api/"):
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                return jsonify({"error": "Enter the demo access token shown in the terminal."}), 401

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        return response

    def state_payload():
        state = runtime.state()
        state["channel"] = "whatsapp" if bridge else "local"
        state["seconds_per_day"] = worker.rate if worker else 0
        state["worker_error"] = worker.last_error if worker else None
        return state

    def deliver():
        if bridge:
            executor.submit(bridge.flush)

    @app.errorhandler(ValueError)
    def bad_input(exc):
        return jsonify({"error": str(exc)}), 400

    @app.get("/")
    def index():
        return render_template("autopay.html")

    @app.get("/healthz")
    def health():
        return jsonify({"ok": True, "bank_mode": "synthetic_local_simulation"})

    @app.get("/api/state")
    def state():
        return jsonify(state_payload())

    def body():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("Send a JSON object")
        return payload

    @app.post("/api/check")
    def check():
        runtime.tick()
        deliver()
        return jsonify(state_payload())

    @app.post("/api/advance")
    def advance():
        runtime.advance(body().get("days", 1))
        deliver()
        return jsonify(state_payload())

    @app.post("/api/clock")
    def clock():
        if not worker:
            raise ValueError("No background clock attached")
        worker.set_rate(body().get("seconds_per_day"))
        return jsonify(state_payload())

    @app.post("/api/reset")
    def reset():
        if worker:
            worker.set_rate(0)
        runtime.reset()
        deliver()
        return jsonify(state_payload())

    @app.post("/api/delay-income")
    def delay():
        runtime.delay_income(body().get("days", 7))
        return jsonify(state_payload())

    @app.post("/api/message")
    def message():
        if bridge:
            return jsonify({"error": "WhatsApp mode accepts approvals from the signed phone webhook. Reply on your phone."}), 409
        data = body()
        ref = data.get("request_id")
        if ref is not None and not isinstance(ref, str):
            raise ValueError("request_id must be a string")
        runtime.receive(data.get("actor"), data.get("text"), data.get("message_id") or uuid.uuid4().hex,
                        request_id=ref)
        return jsonify(state_payload())

    def signed():
        url = bridge.settings.public_base_url + request.path if bridge.settings.public_base_url else request.base_url
        if request.query_string:
            url += "?" + request.query_string.decode("ascii")
        return bridge.twilio.validate_signature(url, request.form, request.headers.get("X-Twilio-Signature"))

    @app.post("/whatsapp/inbound")
    def inbound():
        if not bridge:
            return Response("WhatsApp is not enabled", status=404)
        if not signed():
            return Response("Forbidden", status=403)
        sender = request.form.get("From", "")
        actor = bridge.actor_for(sender)
        if actor is None:
            return Response("<Response/>", mimetype="text/xml")
        sid = request.form.get("MessageSid", "")
        text = request.form.get("Body", "")
        if not sid or len(sid) > 128 or not text.strip() or len(text) > 2000:
            return Response("MessageSid and a text body are required", status=400)
        executor.submit(bridge.inbound, actor, sender, text, sid)
        return Response("<Response/>", mimetype="text/xml")

    @app.post("/whatsapp/status")
    def status():
        if not bridge:
            return Response(status=404)
        return Response(status=204 if signed() else 403)

    return app
