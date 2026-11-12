"""Signed Slack events and caregiver buttons for the single-household demo."""

import json
import logging
import sqlite3
from pathlib import Path

from flask import jsonify, request

log = logging.getLogger(__name__)


def register_slack(app, settings, messenger, executor, reply_to, runtime):
    database = Path(runtime.directory) / "slack-events.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY)")

    def verified():
        return messenger.signed(request.get_data(), request.headers.get("X-Slack-Request-Timestamp"),
                                request.headers.get("X-Slack-Signature"))

    @app.post("/slack/events")
    def slack_events():
        if not verified():
            return jsonify(error="Forbidden"), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Invalid event"), 400
        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or len(challenge) > 1000:
                return jsonify(error="Invalid challenge"), 400
            return jsonify(challenge=challenge)
        if payload.get("team_id") != settings.slack_team_id:
            return jsonify(error="Forbidden"), 403
        event = payload.get("event")
        if not isinstance(event, dict):
            return jsonify(ok=True)
        if (payload.get("type") != "event_callback" or event.get("type") != "message"
                or event.get("channel_type") != "im" or event.get("bot_id")
                or event.get("subtype") or event.get("user") != settings.slack_senior_user_id):
            return jsonify(ok=True)
        event_id, body = payload.get("event_id"), event.get("text", "")
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 120 or not isinstance(body, str):
            return jsonify(error="Invalid event"), 400
        with sqlite3.connect(database, timeout=30) as db:
            fresh = db.execute("INSERT OR IGNORE INTO receipts VALUES (?)", (event_id,)).rowcount
        if fresh:
            try:
                executor.submit(reply_to, settings.recipient, "slack:" + event_id, body)
            except Exception:
                with sqlite3.connect(database) as db:
                    db.execute("DELETE FROM receipts WHERE id=?", (event_id,))
                return jsonify(error="Try again"), 503
            log.info("Accepted Slack DM event")
        return jsonify(ok=True)

    def decide(token, decision):
        with runtime.lock:
            try:
                view, note = runtime.payments.caregiver_decide(token, decision)
                if view is None:
                    text = "This approval is no longer valid. Ask for a fresh proposal."
                else:
                    text = note or view["status_text"]
                    runtime.tick()
                messenger.send_reply(settings.slack_caregiver_user_id, text)
            except Exception as exc:
                log.error("Slack caregiver action failed: %s", type(exc).__name__)

    @app.post("/slack/actions")
    def slack_actions():
        if not verified():
            return jsonify(error="Forbidden"), 403
        try:
            payload = json.loads(request.form.get("payload", ""))
        except (ValueError, TypeError):
            return jsonify(error="Invalid action"), 400
        if not isinstance(payload, dict):
            return jsonify(error="Invalid action"), 400
        team, user = payload.get("team"), payload.get("user")
        if not isinstance(team, dict) or not isinstance(user, dict):
            return jsonify(error="Invalid action"), 400
        if (team.get("id") != settings.slack_team_id
                or not settings.slack_caregiver_user_id
                or settings.slack_caregiver_user_id == settings.slack_senior_user_id
                or user.get("id") != settings.slack_caregiver_user_id):
            return jsonify(error="Forbidden"), 403
        actions = payload.get("actions")
        if payload.get("type") != "block_actions" or not isinstance(actions, list) or len(actions) != 1:
            return jsonify(error="Invalid action"), 400
        action = actions[0]
        if not isinstance(action, dict) or action.get("action_id") not in {"steady_approve", "steady_deny"}:
            return jsonify(error="Invalid action"), 400
        token = action.get("value")
        if not isinstance(token, str) or not 16 <= len(token) <= 128:
            return jsonify(error="Invalid action"), 400
        executor.submit(decide, token, action["action_id"].removeprefix("steady_"))
        return jsonify(ok=True)
