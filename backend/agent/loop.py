"""The one agent loop.

The LLM handles language (intent label, wording). Code handles money and
facts (every Nessie read, every number in a reply).

The default slice is read-only. Optional external-biller mode delegates exact
consent to deterministic code. After that, a tool-calling guide can reply,
email the caregiver, flag a charge, or hold a known payment job. It cannot
invent a job id or send a balance. ``backend.money.contracts`` is deliberately
not imported.
"""

from __future__ import annotations

import logging
import re

from ..nessie.client import CustomerNotFound, NessieClient, NessieError
from . import reply
from .facts import gather_facts
from .guide import guide, open_cosign_alert
from .audit import asks_for_summary
from .intent import ParsedIntent, parse_amount_cents, parse_intent
from .llm import LLM, LLMError
from .photo import InboundMedia, PhotoIntake, confirm_text, simple_decision

log = logging.getLogger(__name__)

_PAYMENT_REF = re.compile(r"\b[0-9A-Fa-f]{8}\b")


def _names_a_payment(body: str) -> bool:
    """True when the text includes an 8-character payment reference."""
    return bool(_PAYMENT_REF.search(body or ""))


class Agent:
    def __init__(self, llm: LLM, nessie: NessieClient, customer_first_name: str, *,
                 payments=None, watcher=None, audit=None, photos: PhotoIntake | None = None):
        self._llm = llm
        self._nessie = nessie
        self._customer = customer_first_name
        self._payments = payments
        self._watcher = watcher
        self._audit = audit
        self._photos = photos

    def _finish(self, kind: str, reason: str, messages: list[str], **detail) -> list[str]:
        if self._audit is not None:
            self._audit.append(kind, reason, **detail)
            for text in messages:
                self._audit.append("reply", "sent to Margaret", text=text)
            self._audit.note_activity()
        return messages

    def respond(self, body: str, *, message_id: str = "",
                media: InboundMedia | None = None) -> list[str]:
        """Turn one inbound text into the reply texts to send, in order.

        ``body`` is untrusted. It only ever reaches the model as a user message.
        A photo is handled before intent: the model reads the picture, code
        saves the bill after YES.
        """
        if media is not None:
            if self._photos is None:
                return self._finish("blocked", "photo intake is not attached", [reply.PHOTO_NOT_ON])
            return self._finish("decision", "read a bill photo", self._photos.ingest(media))

        if self._photos is not None and self._photos.pending() is not None:
            simple = simple_decision(body)
            if simple is not None:
                handled = self._photos.decide(simple)
                if handled is not None:
                    return self._finish("decision", "bill photo reply handled in code", handled,
                                        intent=simple)

        if self._audit is not None and asks_for_summary(body):
            return self._audit.give_recap()

        try:
            parsed = parse_intent(self._llm, body)
        except LLMError as exc:
            log.error("intent parse failed: %s", exc)
            return self._finish("blocked", "intent parse failed", [reply.TROUBLE])

        log.info("intent=%s confidence=%.2f needs=%s", parsed.intent, parsed.confidence,
                 list(parsed.needs))

        named_payment = bool(self._payments is not None and parsed.intent in {"yes", "no", "undo"}
                             and _names_a_payment(body))

        # A bill proposal's YES/NO comes first. An old charge alert must not swallow it.
        if self._payments is not None and parsed.intent in {"yes", "no", "undo"}:
            handled = self._payments.handle(body, parsed.intent, message_id)
            if handled is not None:
                return self._finish("decision", "payment reply handled in code", handled,
                                    intent=parsed.intent)

        if self._photos is not None and parsed.intent in {"yes", "no", "undo"}:
            handled = self._photos.decide(parsed.intent)
            if handled is not None:
                return self._finish("decision", "bill photo reply handled in code", handled,
                                    intent=parsed.intent)

        if self._audit is not None:
            taken = self._audit.take_offer(parsed.intent, named_payment=named_payment)
            if taken is not None:
                return taken
            self._audit.append("intent", f"labeled {parsed.intent}",
                               intent=parsed.intent, confidence=parsed.confidence)

        alert = open_cosign_alert(self._watcher)

        if (self._photos is not None and parsed.intent == "unclear"
                and self._photos.pending() is not None):
            return self._finish("decision", "asked again about the bill photo",
                                [confirm_text(self._photos.pending())])

        if parsed.intent == "question":
            return self._answer_question(body, parsed)
        if parsed.intent == "pay_request":
            return self._finish("blocked", "Paying is not turned on.", [reply.PAY_NOT_ON])
        if parsed.intent == "contemplate":
            return self._contemplate(body, parsed)
        if parsed.intent == "unclear" and alert is None and self._watcher is None:
            return self._finish("decision", "unclear text, asked again", [reply.REASK])
        if self._watcher is not None or self._payments is not None:
            messages = guide(self._llm, body, parsed.intent, watcher=self._watcher,
                             payments=self._payments, audit=self._audit)
            if self._audit is not None:
                self._audit.note_activity()
            return messages
        if parsed.intent == "unclear":
            return self._finish("decision", "unclear text, asked again", [reply.REASK])
        return self._finish("decision", "that action is not available", [reply.NOT_AVAILABLE])

    def _contemplate(self, body: str, parsed: ParsedIntent) -> list[str]:
        """"What if I spend $X at Y?" Code parses X; the judge decides; nothing is stored."""
        amount_cents = parse_amount_cents(body)
        if amount_cents is None:
            return self._finish("contemplate", "needed an amount", [reply.ASK_AMOUNT])
        merchant = parsed.merchant
        if not merchant or merchant.casefold() not in body.casefold():
            return self._finish("contemplate", "needed a payee", [reply.ASK_PAYEE])
        if self._watcher is None:
            return self._finish("contemplate", "that action is not available", [reply.NOT_AVAILABLE])
        try:
            result = self._watcher.check(merchant, amount_cents)
        except CustomerNotFound as exc:
            log.error("%s", exc)
            return self._finish("contemplate", "account was not visible", [reply.CANT_SEE_ACCOUNT])
        except NessieError as exc:
            log.error("Nessie unavailable: %s", exc)
            return self._finish("contemplate", "account was not visible", [reply.CANT_SEE_ACCOUNT])
        return self._finish("contemplate",
                            f"Checked whether {merchant} at that amount would fit.",
                            [result["text"] or reply.CANT_SEE_BALANCE],
                            merchant=merchant)

    def _answer_question(self, body: str, parsed: ParsedIntent) -> list[str]:
        external = []
        if self._payments is not None and "bills" in parsed.needs:
            external = self._payments.bill_summary()
            remaining = tuple(n for n in parsed.needs if n != "bills")
            if not remaining:
                return self._finish("question", "You asked about bills.", external, needs=["bills"])
            parsed = ParsedIntent(parsed.intent,parsed.confidence,remaining,parsed.restatement)
        try:
            account = self._nessie.resolve(self._customer)
            facts = gather_facts(self._nessie, account["_id"], parsed.needs)
            if self._payments is not None and "balance" in parsed.needs:
                facts = self._payments.with_available_balance(account["_id"], facts)
        except CustomerNotFound as exc:
            # Spec: stop and report who does exist. Never create a customer.
            log.error("%s", exc)
            return self._finish("question", "account was not visible", [reply.CANT_SEE_ACCOUNT])
        except NessieError as exc:
            log.error("Nessie unavailable: %s", exc)
            return self._finish("question", "account was not visible", [reply.CANT_SEE_ACCOUNT])

        asked = " and ".join(parsed.needs) or "your account"
        return self._finish("question", f"You asked about {asked}.",
                            external + reply.write_answer(self._llm, body, parsed.restatement,
                                                         facts, parsed.needs),
                            needs=list(parsed.needs))
