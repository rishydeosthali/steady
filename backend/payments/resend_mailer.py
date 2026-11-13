"""Caregiver email over HTTPS, including on hosts that block SMTP."""

import hashlib

import requests

from .caregiver import CaregiverMailer, MailError


class ResendMailer(CaregiverMailer):
    def __init__(self, api_key, sender, caregiver_email, *, base_url,
                 caregiver_name="your caregiver", senior_name="Margaret", timeout=15):
        super().__init__(sender, "", caregiver_email, base_url=base_url,
                         caregiver_name=caregiver_name, senior_name=senior_name)
        if not api_key.strip():
            raise ValueError("RESEND_API_KEY must not be empty")
        self._api_key = api_key.strip()
        self._timeout = timeout

    def request_approval(self, payment, token):
        # Retries use the same key without putting the approval credential in headers.
        key = "caregiver/" + hashlib.sha256(token.encode()).hexdigest()
        self._send(self.compose(payment, token), idempotency_key=key)

    def _send(self, message, *, idempotency_key=None):
        payload = {"from": str(message["From"]), "to": [self.caregiver_email],
                   "subject": str(message["Subject"])}
        for kind, field in (("plain", "text"), ("html", "html")):
            body = message.get_body((kind,))
            if body is not None:
                payload[field] = body.get_content()
        headers = {"Authorization": "Bearer " + self._api_key}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with requests.post("https://api.resend.com/emails", json=payload,
                               headers=headers, timeout=self._timeout,
                               allow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    raise MailError(f"Caregiver email failed: Resend HTTP {response.status_code}")
                result = response.json()
                if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
                    raise MailError("Caregiver email failed: Resend response missing message ID")
        except (requests.RequestException, ValueError) as exc:
            # Provider errors can contain addresses, approval links, or credentials.
            raise MailError("Caregiver email failed: Resend " + type(exc).__name__) from None
