"""Caregiver approval by email.

The email and the approval page show only the payee, amount, and proposed
date. They never include the senior's balance, deposits, or cushion.
Opening a link only shows the request; a decision needs a button press (POST),
so mail scanners that prefetch links cannot approve or deny anything.
"""

from __future__ import annotations

import hashlib
import html
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit

GMAIL_SMTP = ("smtp.gmail.com", 587)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def binding(payee: str, amount_cents: int, payment_date: str) -> str:
    """Ties an approval to the exact payee, amount, and date it was asked about."""
    return hashlib.sha256(f"{payee.casefold()}|{amount_cents}|{payment_date}".encode()).hexdigest()


class MailError(RuntimeError):
    pass


class CaregiverMailer:
    def __init__(self, sender, app_password, caregiver_email, *, base_url, caregiver_name="your caregiver",
                 senior_name="Margaret", smtp_factory=None, timeout=15):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("Approval base URL must be an http(s) origin")
        if "@" not in sender or "@" not in caregiver_email:
            raise ValueError("Sender and caregiver must be email addresses")
        self.sender, self.caregiver_email = sender, caregiver_email
        self._password = app_password.replace(" ", "")
        self.base_url = base_url.rstrip("/")
        self.caregiver_name, self.senior_name = caregiver_name, senior_name
        self._smtp_factory = smtp_factory or (lambda: smtplib.SMTP(*GMAIL_SMTP, timeout=timeout))

    def __repr__(self):
        return f"CaregiverMailer(sender={self.sender!r}, caregiver={self.caregiver_email!r})"

    def link(self, token: str, decision: str | None = None) -> str:
        return f"{self.base_url}/caregiver/{token}" + (f"?decision={decision}" if decision else "")

    def compose(self, payment: dict, token: str) -> EmailMessage:
        payee, amount, day = payment["payee"], payment["amount"], payment["date"]
        approve, deny = self.link(token, "approve"), self.link(token, "deny")
        message = EmailMessage()
        message["Subject"] = f"Steady: please approve {self.senior_name}'s {payee} payment"
        message["From"] = f"Steady <{self.sender}>"
        message["To"] = self.caregiver_email
        message.set_content(
            f"{self.senior_name} asked Steady to pay a bill and wants your approval.\n\n"
            f"Payee: {payee}\nAmount: {amount}\nPayment date: {day}\n\n"
            f"Approve: {approve}\nDeny: {deny}\n\n"
            "Nothing is paid unless you approve. If you do nothing, the payment stays on hold.\n"
            "This is a Steady demo payment to a simulated biller; no real funds move.\n")
        e = html.escape
        button = ("display:inline-block;padding:12px 22px;margin-right:12px;border-radius:6px;"
                  "color:#fff;text-decoration:none;font-weight:bold;background:")
        message.add_alternative(f"""\
<html><body style="font-family:Arial,sans-serif;font-size:16px;color:#222">
<p>{e(self.senior_name)} asked Steady to pay a bill and wants your approval.</p>
<table style="border-collapse:collapse;margin:12px 0">
<tr><td style="padding:4px 16px 4px 0"><b>Payee</b></td><td>{e(payee)}</td></tr>
<tr><td style="padding:4px 16px 4px 0"><b>Amount</b></td><td>{e(amount)}</td></tr>
<tr><td style="padding:4px 16px 4px 0"><b>Payment date</b></td><td>{e(day)}</td></tr>
</table>
<p><a href="{e(approve)}" style="{button}#1e7d32">Approve</a>
<a href="{e(deny)}" style="{button}#b3261e">Deny</a></p>
<p>Nothing is paid unless you approve. If you do nothing, the payment stays on hold.</p>
<p style="color:#666;font-size:13px">This is a Steady demo payment to a simulated biller; no real funds move.</p>
</body></html>""", subtype="html")
        return message

    def request_approval(self, payment: dict, token: str) -> None:
        message = self.compose(payment, token)
        try:
            with self._smtp_factory() as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(self.sender, self._password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            # Type only: SMTP errors can echo account details.
            raise MailError("Caregiver email failed: " + type(exc).__name__) from None


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Steady payment approval</title>
<style>body{{font-family:Arial,sans-serif;max-width:520px;margin:40px auto;padding:0 16px;font-size:18px;color:#222}}
table{{margin:16px 0}}td{{padding:4px 16px 4px 0}}button{{font-size:18px;padding:12px 24px;margin-right:12px;border:0;border-radius:6px;color:#fff;cursor:pointer}}
.approve{{background:#1e7d32}}.deny{{background:#b3261e}}.hint{{color:#666;font-size:14px}}.picked{{outline:4px solid #f5b400}}</style>
</head><body><h1>Steady payment approval</h1>{body}</body></html>"""


def render_page(view: dict, token: str, *, preselect: str | None = None, message: str | None = None) -> str:
    e = html.escape
    details = (f"<table><tr><td><b>Payee</b></td><td>{e(view['payee'])}</td></tr>"
               f"<tr><td><b>Amount</b></td><td>{e(view['amount'])}</td></tr>"
               f"<tr><td><b>Payment date</b></td><td>{e(view['date'])}</td></tr></table>")
    if message:
        return _PAGE.format(body=f"<p>{e(message)}</p>{details}")
    if view["status"] != "awaiting_caregiver":
        return _PAGE.format(body=f"<p>{e(view['status_text'])}</p>{details}")
    picked = {d: " picked" if preselect == d else "" for d in ("approve", "deny")}
    forms = "".join(
        f'<form method="post" action="/caregiver/{e(token)}/{d}" style="display:inline">'
        f'<button class="{d}{picked[d]}" type="submit">{label}</button></form>'
        for d, label in (("approve", "Approve"), ("deny", "Deny")))
    return _PAGE.format(body=(f"<p>{e(view['senior'])} wants to make this payment and needs your approval.</p>"
                              f"{details}{forms}<p class='hint'>Nothing is paid unless you approve. "
                              "Demo payment to a simulated biller; no real funds move.</p>"))
