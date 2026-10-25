"""Local GET responses for the existing NessieClient. Never calls a network."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from werkzeug.serving import WSGIRequestHandler

from ..nessie.client import NessieClient

FIXTURE_BASE_URL = "https://synthetic.nessie.invalid"


class FixtureRequestHandler(WSGIRequestHandler):
    """Do not log query strings: NessieClient sends an API key in one."""

    def log_request(self, code="-", size="-") -> None:
        self.log("info", "%s %s %s", self.command, urlsplit(self.path).path, code)


def read_routes(snapshot: dict[str, Any]) -> dict[str, Any]:
    customer, account = snapshot["customer"], snapshot["account"]
    return {
        "/customers": [customer],
        f"/customers/{customer['_id']}": customer,
        f"/customers/{customer['_id']}/accounts": [account],
        f"/accounts/{account['_id']}": account,
        f"/accounts/{account['_id']}/bills": snapshot["bills"],
        f"/accounts/{account['_id']}/deposits": snapshot["deposits"],
        f"/accounts/{account['_id']}/purchases": snapshot["purchases"],
        "/merchants": snapshot["merchants"],
    }


class _Response:
    def __init__(self, status: int, payload: Any):
        self.status_code, self.ok = status, status == 200
        self._payload = payload

    def json(self) -> Any:
        return deepcopy(self._payload)


class FixtureSession:
    """A fresh copy per run; modifying a returned record cannot contaminate it."""

    def __init__(self, snapshot: dict[str, Any]):
        self._routes = deepcopy(read_routes(snapshot))

    def get(self, url: str, params=None, timeout=None) -> _Response:
        parsed = urlsplit(url)
        base = urlsplit(FIXTURE_BASE_URL)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise ValueError("FixtureSession only supports the synthetic .invalid origin")
        path = parsed.path
        if path not in self._routes:
            return _Response(404, {})
        return _Response(200, self._routes[path])


def fixture_client(snapshot: dict[str, Any]) -> NessieClient:
    """Inject into Agent(nessie=...) to exercise its real read/formatting code."""
    return NessieClient(FIXTURE_BASE_URL, "synthetic-not-a-secret", session=FixtureSession(snapshot))


def create_fixture_app(snapshot: dict[str, Any]):
    """Loopback-only server selected explicitly by the demo CLI, GET only.

    Point NESSIE_BASE_URL at it to use the existing WhatsApp server unchanged.
    Payment endpoints are intentionally absent. Expectations are never exposed.
    """
    from flask import Flask, jsonify

    app = Flask("steady_synthetic_nessie")
    routes = deepcopy(read_routes(snapshot))

    @app.get("/healthz")
    def healthz():
        return jsonify({"synthetic": True, "scenario": snapshot["scenario_id"]})

    @app.get("/<path:path>")
    def read(path: str):
        route = "/" + path
        if route not in routes:
            return jsonify({"message": "No synthetic record for this path"}), 404
        return jsonify(routes[route])

    return app
