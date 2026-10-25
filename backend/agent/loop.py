"""The one agent loop.

The LLM handles language (intent label, wording). Code handles money and
facts (every Nessie read, every number in a reply).

This slice is read-only. No branch here creates, cancels, or approves a
payment, and ``backend.money.contracts`` is deliberately not imported.
"""

from __future__ import annotations

import logging

from ..nessie.client import CustomerNotFound, NessieClient, NessieError
from . import reply
from .facts import gather_facts
from .intent import ParsedIntent, parse_intent
from .llm import LLM, LLMError

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, llm: LLM, nessie: NessieClient, customer_first_name: str):
        self._llm = llm
        self._nessie = nessie
        self._customer = customer_first_name

    def respond(self, body: str) -> list[str]:
        """Turn one inbound text into the reply texts to send, in order.

        ``body`` is untrusted. It only ever reaches the model as a user message.
        """
        try:
            parsed = parse_intent(self._llm, body)
        except LLMError as exc:
            log.error("intent parse failed: %s", exc)
            return [reply.TROUBLE]

        log.info("intent=%s confidence=%.2f needs=%s", parsed.intent, parsed.confidence,
                 list(parsed.needs))

        if parsed.intent == "unclear":
            return [reply.REASK]                       # no Nessie call
        if parsed.intent == "question":
            return self._answer_question(body, parsed)
        if parsed.intent == "pay_request":
            return [reply.PAY_NOT_ON]                  # never pays
        # yes / no / undo / forwarded_message: recognised, not handled in this slice.
        return [reply.NOT_AVAILABLE]

    def _answer_question(self, body: str, parsed: ParsedIntent) -> list[str]:
        try:
            account = self._nessie.resolve(self._customer)
            facts = gather_facts(self._nessie, account["_id"], parsed.needs)
        except CustomerNotFound as exc:
            # Spec: stop and report who does exist. Never create a customer.
            log.error("%s", exc)
            return [reply.CANT_SEE_ACCOUNT]
        except NessieError as exc:
            log.error("Nessie unavailable: %s", exc)
            return [reply.CANT_SEE_ACCOUNT]

        return reply.write_answer(self._llm, body, parsed.restatement, facts, parsed.needs)
