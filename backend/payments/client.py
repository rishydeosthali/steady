"""Fixed, authenticated external biller contract. No model-chosen URLs or money."""

from urllib.parse import urlsplit

import requests


class BillerError(RuntimeError):
    pass


class BillerClient:
    def __init__(self, base_url, token, *, session=None, timeout=5):
        parsed = urlsplit(base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use an HTTPS biller URL, or HTTP on loopback for the local simulator")
        if len(token) < 16:
            raise ValueError("BILLER_API_TOKEN must be at least 16 characters")
        self.base = base_url.rstrip("/")
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def _request(self, method, path, *, missing_ok=False, **kwargs):
        try:
            response = self.session.request(method, self.base + path,
                headers={"Authorization":"Bearer " + self.token, **kwargs.pop("headers", {})},
                timeout=self.timeout, allow_redirects=False, **kwargs)
        except requests.RequestException as exc:
            raise BillerError("Biller transport failed: " + type(exc).__name__) from None
        if response.status_code == 404 and missing_ok:
            return None
        if not 200 <= response.status_code < 300:
            raise BillerError(f"Biller returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise BillerError("Biller returned invalid JSON") from None
        if not isinstance(payload, dict) or payload.get("simulation") is not True:
            raise BillerError("Biller must explicitly identify itself as a simulation")
        return payload

    def invoices(self, customer):
        payload = self._request("GET", "/v1/invoices", params={"customer_ref":customer})
        rows = payload.get("invoices")
        if not isinstance(rows, list):
            raise BillerError("Invalid invoice list")
        return rows

    def invoice(self, invoice_id):
        if not isinstance(invoice_id, str) or not invoice_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in invoice_id):
            raise BillerError("Invalid invoice id")
        return self._request("GET", "/v1/invoices/" + invoice_id)["invoice"]

    def receipt(self, key):
        if not key.isalnum():
            raise BillerError("Invalid idempotency key")
        return self._request("GET", "/v1/payments/" + key, missing_ok=True)

    def pay(self, key, payload):
        return self._request("POST", "/v1/payments", headers={"Idempotency-Key":key}, json=payload)
