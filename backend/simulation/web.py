"""Authenticated operator UI. Public webhooks retain Twilio signature checks."""
import hmac
import time
from pathlib import Path

from flask import jsonify, request, send_from_directory

from ..config import ConfigError
from .runtime import Conflict

UI = Path(__file__).parent / "ui"


def register_dashboard(app, runtime, token):
    if not isinstance(token, str) or len(token) < 32:
        raise ConfigError("STEADY_DASHBOARD_TOKEN must be at least 32 characters")
    failures = {}

    @app.before_request
    def dashboard_auth():
        if not request.path.startswith("/simulation/api/"):
            return None
        now = time.monotonic()
        for addr, value in list(failures.items()):
            if now - value[1] > 60:
                failures.pop(addr, None)
        addr = request.remote_addr
        count, since = failures.get(addr, (0, now))
        if count >= 20:
            return jsonify(error="Too many attempts. Try again in a minute."), 429
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            failures[addr] = (count+1, since)
            return jsonify(error="Enter the dashboard access key from your host's environment settings."), 401

    @app.after_request
    def dashboard_headers(response):
        if request.path == "/" or request.path.startswith("/simulation"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.get("/")
    @app.get("/simulation")
    def simulation_page():
        return send_from_directory(UI, "index.html")

    @app.get("/simulation/static/<path:name>")
    def simulation_static(name):
        return send_from_directory(UI, name)

    @app.get("/simulation/api/state")
    def simulation_state():
        return jsonify(runtime.state())

    @app.post("/simulation/api/<operation>")
    def simulation_action(operation):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Send a JSON object"), 400
        try:
            with runtime.lock:
                if operation in {"reset", "save", "load"}:
                    if body.get("run_id") != runtime.store.state()["run_id"]:
                        raise Conflict("This run has changed. Refresh and try again.")
                    if operation == "save":
                        key = request.headers.get("Idempotency-Key", "")
                        if not 8 <= len(key) <= 100:
                            raise ValueError("A request id is required")
                        runtime.save(body.get("name"), key)
                    else:
                        if operation == "load" and not body.get("saved_id"):
                            raise ValueError("Choose a saved scenario")
                        runtime.reset(body.get("template", "comfortable"),
                                      saved_id=body.get("saved_id") if operation == "load" else None)
                else:
                    runtime.command(operation, body, request.headers.get("Idempotency-Key"))
                return jsonify(runtime.state())
        except Conflict as exc:
            return jsonify(error=str(exc)), 409
        except (ValueError, TypeError) as exc:
            return jsonify(error=str(exc)), 400
