"""Private SQLite memory and a cross-process lease for the controller."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Memory:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS lease (id INTEGER PRIMARY KEY, owner TEXT, expires REAL)")
            initial = dict(goals={}, drafts={}, reports={}, outbox=[], events=[], snapshots=[],
                           next_check=0, fingerprint="", inbound={})
            db.execute("INSERT OR IGNORE INTO memory VALUES(1, ?)", (json.dumps(initial),))
            db.execute("INSERT OR IGNORE INTO lease VALUES(1, '', 0)")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT body FROM memory WHERE id=1").fetchone()[0])
            yield state
            state["events"] = state["events"][-200:]
            state["snapshots"] = state["snapshots"][-12:]
            db.execute("UPDATE memory SET body=? WHERE id=1", (json.dumps(state),))
            db.commit()
        finally:
            db.close()

    def read(self):
        with self.transaction() as state:
            return json.loads(json.dumps(state))

    @contextmanager
    def lease(self):
        owner = uuid.uuid4().hex
        with sqlite3.connect(self.path, timeout=30) as db:
            claimed = db.execute("UPDATE lease SET owner=?, expires=? WHERE id=1 AND expires<?",
                                 (owner, time.time() + 600, time.time())).rowcount
        try:
            yield bool(claimed)
        finally:
            if claimed:
                with sqlite3.connect(self.path, timeout=30) as db:
                    db.execute("UPDATE lease SET expires=0 WHERE id=1 AND owner=?", (owner,))

    def event(self, kind, **detail):
        with self.transaction() as state:
            state["events"].append({"at": time.time(), "kind": kind, **detail})

    def queue(self, key, text):
        with self.transaction() as state:
            if not any(item["key"] == key for item in state["outbox"]):
                family = ":".join(key.split(":")[:2]) + ":"
                for item in state["outbox"]:
                    if item["status"] == "pending" and item["key"].startswith(family):
                        item["status"] = "superseded"
                state["outbox"].append(dict(key=key, text=text, status="pending", attempts=0))

    def protected_buffer(self):
        return max([20000] + [g["buffer_cents"] for g in self.read()["goals"].values()
                             if g["kind"] == "budget" and g["status"] in {"active", "paused", "needs_review"}])

    def daily_essentials(self):
        goals = [g for g in self.read()["goals"].values()
                 if g["kind"] == "budget" and g["status"] in {"active", "paused", "needs_review"}]
        return max([0] + [((g.get("weekly_essentials_cents") or 0) + 6) // 7 for g in goals])
