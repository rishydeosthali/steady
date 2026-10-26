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
        message.add_alternative(f"""\
<html><body style="margin:0;padding:0;background:#f3f1ea">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f1ea;padding:28px 12px">
<tr><td align="center">
<table role="presentation" width="440" cellpadding="0" cellspacing="0" style="max-width:440px;background:#fffef8;border:1px solid #d8dece;border-radius:22px">
<tr><td style="padding:22px 26px 8px;font-family:Georgia,serif;font-size:26px;font-weight:700;letter-spacing:-1px;color:#213c34">steady<span style="color:#c7a95d;font-size:11px"> ●</span></td></tr>
<tr><td style="padding:0 26px 6px;font-family:Arial,sans-serif;font-size:11px;letter-spacing:1.6px;font-weight:700;color:#65806d">NEEDS YOUR OK</td></tr>
<tr><td style="padding:0 26px 8px;font-family:Georgia,serif;font-size:26px;line-height:1.2;color:#213c34">{e(self.senior_name)} wants to pay a bill</td></tr>
<tr><td style="padding:0 26px 18px;font-family:Arial,sans-serif;font-size:15px;line-height:1.5;color:#5b6b5e">Steady is waiting on you before anything is sent.</td></tr>
<tr><td style="padding:0 22px 8px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7f6ef;border:1px solid #e4ead8;border-radius:16px">
<tr><td style="padding:18px 20px 6px;font-family:Arial,sans-serif;font-size:11px;letter-spacing:.8px;color:#7a8776">AMOUNT</td></tr>
<tr><td style="padding:0 20px 14px;font-family:Georgia,serif;font-size:36px;letter-spacing:-1px;color:#213c34">{e(amount)}</td></tr>
<tr><td style="padding:0 20px 14px;border-top:1px solid #e8eedc">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td style="padding-top:14px;font-family:Arial,sans-serif;font-size:12px;color:#7a8776">Payee<br><b style="font-size:16px;color:#213c34">{e(payee)}</b></td>
<td align="right" style="padding-top:14px;font-family:Arial,sans-serif;font-size:12px;color:#7a8776">Payment date<br><b style="font-size:16px;color:#213c34">{e(day)}</b></td></tr>
</table></td></tr>
</table></td></tr>
<tr><td style="padding:18px 22px 8px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="50%" style="padding-right:6px"><a href="{e(approve)}" style="display:block;text-align:center;padding:14px 10px;border-radius:12px;background:#264f40;color:#fff;font-family:Arial,sans-serif;font-size:16px;font-weight:700;text-decoration:none">Approve</a></td>
<td width="50%" style="padding-left:6px"><a href="{e(deny)}" style="display:block;text-align:center;padding:14px 10px;border-radius:12px;background:#fffef8;color:#8d5b32;border:1px solid #e4d2b8;font-family:Arial,sans-serif;font-size:16px;font-weight:700;text-decoration:none">Deny</a></td>
</tr></table></td></tr>
<tr><td style="padding:8px 26px 24px;font-family:Arial,sans-serif;font-size:13px;line-height:1.55;color:#7a8776">Nothing is paid unless you approve. If you do nothing, the payment stays on hold.<br>This is a Steady demo payment to a simulated biller; no real funds move.</td></tr>
</table>
</td></tr></table>
</body></html>""", subtype="html")
        return message

    def request_approval(self, payment: dict, token: str) -> None:
        self._send(self.compose(payment, token))

    def notify(self, reason: str, details: dict, urgency: str = "soon") -> None:
        """Alert email. Payee, amount, and date only. Never a balance."""
        allowed = ("payee", "merchant", "amount", "date", "what_she_said", "kind")
        rows = [(key, details[key]) for key in allowed if details.get(key)]
        if not rows:
            raise MailError("Caregiver email failed: empty notification")
        urgency = urgency if urgency in {"now", "soon", "later"} else "soon"
        reason = (reason or "Margaret needs help").strip()[:80]
        lines = [f"{self.senior_name} said something Steady wants you to see.\n",
                 f"Why: {reason}", f"Urgency: {urgency}\n"]
        lines += [f"{label.replace('_', ' ').title()}: {value}" for label, value in rows]
        lines += ["\nNo account balance is included. This is not a payment approval.",
                  "This is a Steady demo notice; no real funds move."]
        message = EmailMessage()
        message["Subject"] = f"Steady: {self.senior_name} needs help — {reason}"
        message["From"] = f"Steady <{self.sender}>"
        message["To"] = self.caregiver_email
        message.set_content("\n".join(lines))
        e = html.escape
        cells = "".join(f"<tr><td style='padding:4px 16px 4px 0'><b>{e(k.replace('_',' ').title())}</b></td>"
                        f"<td>{e(str(v))}</td></tr>" for k, v in rows)
        message.add_alternative(f"""\
<html><body style="font-family:Arial,sans-serif;font-size:16px;color:#222">
<p>{e(self.senior_name)} said something Steady wants you to see.</p>
<p><b>Why:</b> {e(reason)}<br><b>Urgency:</b> {e(urgency)}</p>
<table style="border-collapse:collapse;margin:12px 0">{cells}</table>
<p>No account balance is included. This is not a payment approval.</p>
<p style="color:#666;font-size:13px">This is a Steady demo notice; no real funds move.</p>
</body></html>""", subtype="html")
        self._send(message)

    def _send(self, message: EmailMessage) -> None:
        try:
            with self._smtp_factory() as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(self.sender, self._password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            # Type only: SMTP errors can echo account details.
            raise MailError("Caregiver email failed: " + type(exc).__name__) from None


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Steady payment approval</title>
<style>
html{{font-family:"Segoe UI",Helvetica,Arial,sans-serif;color:#111;background:#fff}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:flex;justify-content:center;padding:48px 20px;background:#fff}}
.wrap{{width:100%;max-width:420px}}
.brand{{margin:0 0 36px;font-size:15px;font-weight:600}}
h1{{margin:0 0 8px;font-size:22px;font-weight:600;line-height:1.25}}
.lead,.note{{margin:0 0 24px;font-size:14px;line-height:1.5;color:#444}}
table{{width:100%;border-collapse:collapse;margin:0 0 28px}}
th,td{{padding:11px 0;border-bottom:1px solid #ddd;font-size:14px;text-align:left;vertical-align:top}}
th{{width:38%;font-weight:400;color:#555}}
td{{font-weight:600}}
.actions{{display:flex;gap:12px;margin:0 0 20px}}
form{{flex:1;margin:0}}
button{{width:100%;padding:10px 14px;border:1px solid #111;border-radius:2px;font:inherit;font-size:14px;font-weight:600;cursor:pointer}}
.approve{{background:#111;color:#fff}}
.approve:hover{{background:#000}}
.deny{{background:#fff;color:#111}}
.deny:hover{{background:#f4f4f4}}
.picked{{outline:2px solid #111;outline-offset:2px}}
.hint{{margin:0;font-size:13px;line-height:1.5;color:#555}}
.status{{margin:0 0 8px;font-size:12px;font-weight:600;color:#555}}
</style>
</head>
<body><div class="wrap"><p class="brand">Steady</p>{body}</div></body></html>"""


def _details(view: dict) -> str:
    e = html.escape
    return (f"<table><tbody>"
            f"<tr><th>Amount</th><td>{e(view['amount'])}</td></tr>"
            f"<tr><th>Payee</th><td>{e(view['payee'])}</td></tr>"
            f"<tr><th>Payment date</th><td>{e(view['date'])}</td></tr>"
            f"</tbody></table>")


def render_page(view: dict, token: str, *, preselect: str | None = None, message: str | None = None) -> str:
    e = html.escape
    details = _details(view)
    if message or view["status"] != "awaiting_caregiver":
        note = message or view["status_text"]
        return _PAGE.format(body=(f"<p class='status'>Payment request</p>"
                                  f"<h1>Steady payment approval</h1>"
                                  f"<p class='note'>{e(note)}</p>{details}"))
    picked = {d: " picked" if preselect == d else "" for d in ("approve", "deny")}
    forms = "".join(
        f'<form method="post" action="/caregiver/{e(token)}/{d}">'
        f'<button class="{d}{picked[d]}" type="submit">{label}</button></form>'
        for d, label in (("approve", "Approve"), ("deny", "Deny")))
    return _PAGE.format(body=(f"<h1>Payment approval</h1>"
                              f"<p class='lead'>{e(view['senior'])} asked Steady to pay this bill "
                              f"and needs your approval.</p>"
                              f"{details}<div class='actions'>{forms}</div>"
                              f"<p class='hint'>Nothing is paid unless you approve. "
                              "If you do nothing, the payment stays on hold. "
                              "This is a Steady demo payment to a simulated biller; no real funds move.</p>"))
