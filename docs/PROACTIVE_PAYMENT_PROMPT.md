# Implementation prompt: Proactive outreach and authorized payment execution

Read `docs/FEATURES_AND_GOALS.md`, `docs/PROJECT_CONTEXT.md`, and the existing
WhatsApp implementation before changing code.

Build Steady's strongest demo path:

1. Steady discovers an upcoming bill without Margaret asking.
2. Deterministic code chooses the earliest safe on-time payment date.
3. Steady contacts Margaret first and explains the recommendation.
4. Margaret explicitly confirms a read-back of the exact payee, amount, and date.
5. If required, Sarah separately approves the flagged item.
6. One idempotent Nessie write schedules the payment.
7. Margaret receives a receipt, and every transition is recorded.

Call this **authorized execution**, not autonomous approval. The system performs
the work after consent; it never invents consent.

## Non-negotiable correction to the example

A proposed date must be on or before the due date. Never suggest Friday after a
bill due Monday. If the only deposit arrives after the due date and paying
earlier would break the buffer, return `exception`; do not disguise a late
payment as a recommendation.

The co-sign threshold is $500. Therefore a $600 electric bill cannot execute
from Margaret's “yes” alone. It must also be approved by Sarah. Use the seeded
$120 Austin Energy bill for the fast single-approval demo. Use a separate bill
over $500 to demonstrate Sarah's gate.

## Architecture rule

The LLM handles intent and wording. Deterministic code owns:

- Nessie reads and writes
- date and cash-flow arithmetic
- known-payee lookup
- the $200 protected buffer
- the $500 co-sign threshold
- authorization state
- idempotency
- expiration, retries, and audit records

The LLM may not choose a payment date, approve a payment, construct a Nessie
URL, or call the executor directly.

## Demo experience

For the seeded Austin Energy bill, the ideal conversation is:

> Steady: Your Austin Energy bill is $120.00 and is due Oct 31. I suggest
> paying Oct 31 so your balance stays above the $200.00 you keep back.
> Would you like me to prepare that?

> Margaret: Yes.

> Steady: Please confirm: schedule $120.00 to Austin Energy for Oct 31?
> Reply YES to schedule it, or NO to stop.

> Margaret: Yes.

> Steady: Done. Austin Energy is scheduled for Oct 31 for $120.00.
> You can say “undo” before Oct 31.

The first “yes” accepts the recommendation. The second “yes” confirms the exact
read-back. This deliberate two-step interaction makes the safety model visible
to judges and prevents an old or ambiguous “yes” from moving money.

For a $600 known bill, after Margaret confirms the read-back:

> Steady to Margaret: This payment needs Sarah's approval because it is over
> $500.00. I have asked her. Nothing has been scheduled yet.

> Steady to Sarah: Margaret wants to schedule $600.00 to Austin Energy for
> Oct 31. It needs your approval because it is over $500.00. Reply APPROVE or
> DENY.

Sarah sees only the flagged payment. Never send her Margaret's balance,
deposit amount, protected buffer, or spending history.

## Components

Implement logical components, not separate LLM agents:

```text
Scheduler
  -> Upcoming-bill reader
  -> Deterministic date planner
  -> Payment gate
  -> Authorization state machine
  -> Idempotent Nessie executor
  -> WhatsApp delivery
  -> Audit log
```

Use the existing `NessieClient`, agent loop, Twilio client, and Flask app where
appropriate. Keep pure money rules separate from Flask, Twilio, and the LLM.

Suggested files:

```text
backend/money/upcoming.py       # upcoming bill selection
backend/money/planner.py        # deterministic safe-date calculation
backend/money/gate.py           # allow / needs_sarah / blocked
backend/money/executor.py       # the only Nessie write boundary
backend/payments/state.py       # state transitions and SQLite repository
backend/outreach/service.py     # runs one outreach cycle
backend/outreach/wording.py     # fallback copy and grounded rewrite
backend/outreach/run.py         # manual demo trigger
tests/test_planner.py
tests/test_payment_state.py
tests/test_executor.py
tests/test_outreach.py
```

Replace the stubs in `backend/money/contracts.py` with calls into these modules.

## Payment state machine

Persist payment requests in SQLite. Do not keep authorization only in memory.

States:

```text
discovered
planned
outreach_sent
recommendation_accepted
awaiting_mom_confirmation
awaiting_sarah
authorized
executing
scheduled
denied
blocked
expired
cancelled
exception
failed
```

Allowed path for a known bill under $500:

```text
discovered
-> planned
-> outreach_sent
-> recommendation_accepted
-> awaiting_mom_confirmation
-> authorized
-> executing
-> scheduled
```

Allowed path for a new payee or amount over $500:

```text
...
-> awaiting_mom_confirmation
-> awaiting_sarah
-> authorized
-> executing
-> scheduled
```

Rules:

- A state transition is a compare-and-set transaction. Two webhook workers
  cannot both advance the same request.
- `yes` is valid only for the sender, request, and state currently awaiting it.
- Margaret cannot satisfy Sarah's approval, and Sarah cannot satisfy Margaret's.
- Sarah's approval is valid only after Margaret confirms the read-back.
- A denial is terminal. Tell Margaret the payment is on hold.
- Authorization expires after 15 minutes for the demo. A late reply receives:
  “That request expired. I have not scheduled anything.”
- A bill whose amount, due date, status, or payee changes after outreach
  invalidates the authorization. Re-plan and send a fresh read-back.
- A request already `scheduled`, `denied`, `cancelled`, or `expired` ignores
  duplicate replies and returns its current status.
- Never infer approval from “okay” when more than one request is awaiting a
  decision. Ask which bill.

Every inbound reply must resolve to exactly one open request. Prefer a short
request token in the message and stored conversation context. Never apply a
free-floating “yes” to the newest database row by accident.

## Upcoming-bill scheduler

Expose:

```python
run_outreach(clock: date) -> list[OutreachResult]
```

- Default window: 14 calendar days.
- Include pending bills with due dates from `clock` through
  `clock + 14 days`, inclusive.
- Exclude completed, cancelled, and past-due bills.
- Dedupe by Nessie bill id. Re-running the scheduler must not resend the same
  unchanged outreach.
- If amount, payee, due date, or status changes, invalidate the old request and
  evaluate a new version.
- A manual `POST /outreach/run` and
  `python -m backend.outreach.run` invoke the same path as a future timer.
- Do not add an in-process infinite scheduler for the hackathon.

Store `last_outreach_at`, `bill_version`, and a deterministic dedupe key:

```text
outreach:{account_id}:{bill_id}:{amount_cents}:{due_date}
```

## Deterministic safe-date planner

Input:

- current balance
- target bill and due date
- other pending bills
- pending deposits
- protected buffer: 20,000 cents
- injected `clock`

All arithmetic uses integer cents converted with `Decimal`. No floats.

Evaluate each date from `clock` through the bill's due date, inclusive. For each
candidate date:

1. Begin with the current Nessie balance.
2. Apply pending deposits dated on or before the candidate.
3. Subtract other pending bills dated on or before the candidate.
4. Subtract the target bill exactly once.
5. The candidate is safe only when the resulting balance is strictly greater
   than $200.00.

Return the earliest safe candidate:

```json
{
  "status": "safe",
  "payment_date": "2026-10-31",
  "balance_after_cents": 192775,
  "reason_code": "buffer_preserved",
  "facts_used": {
    "balance_cents": 221300,
    "target_bill_cents": 12000,
    "deposit_ids": [],
    "other_bill_ids": []
  }
}
```

If none exists on or before the due date:

```json
{
  "status": "exception",
  "reason_code": "no_safe_on_time_date"
}
```

On `exception`, do not ask Margaret to approve and do not execute. Text
Margaret and notify Sarah:

> Steady to Margaret: I could not find an on-time date for Austin Energy that
> keeps your $200.00 cushion. I have not scheduled anything.

Sarah's version omits the balance and buffer:

> Austin Energy does not have a safe on-time payment date. Nothing was
> scheduled.

Important: verify how Nessie models pending deposits before relying on them.
Do not assume a pending deposit changes the account balance already returned by
Nessie. Write one documented adapter rule and test it to prevent double counting.

## Payment gate

Pure function:

```python
gate_payment(
    *,
    payee_key: str,
    known_payee_keys: set[str],
    amount_cents: int,
    plan_status: str,
) -> GateDecision
```

Returns:

- `allow`: known payee, amount at or below $500, safe plan
- `needs_sarah`: new payee or amount over $500, safe plan
- `blocked`: unsafe plan, malformed amount/date, bill no longer pending, or a
  conflicting terminal request

The existing spec says “under $500” but also says “over $500” needs Sarah,
leaving exactly $500 ambiguous. For this implementation, lock the boundary as:
**$500.00 is allowed; $500.01 needs Sarah.** Put this in tests and the product
docs.

Known payees come from completed Nessie bill history, normalized by exact
casefold/punctuation rules. Do not use fuzzy matching. A merchant purchase is
not enough to establish a bill payee.

## Read-back and confirmation binding

Before any write, send a deterministic read-back containing:

- exact payee
- exact amount
- exact payment date
- explicit instruction to reply YES or NO

Store a hash of those three fields on the authorization:

```text
sha256(normalized_payee | amount_cents | ISO_payment_date)
```

Immediately before execution:

1. Reload the payment request in a transaction.
2. Confirm the state is `authorized`.
3. Reload the source bill from Nessie.
4. Confirm payee, amount, due date, and status still match.
5. Recompute the safe-date result with fresh account data.
6. Confirm the authorization hash still matches.
7. Atomically claim the idempotency key and move to `executing`.
8. Perform one Nessie write.
9. Store the Nessie response id and move to `scheduled`.

If any check changes, do not execute. Move to `blocked` or restart planning,
and explain that the bill changed.

## Nessie executor

This is the only module allowed to perform a Nessie write.

Before implementation, confirm the current Nessie Swagger behavior for:

- creating/scheduling a bill payment
- required fields
- response id
- cancelling a pending payment

Do not guess endpoint behavior. Keep the transport behind an adapter so tests
use a fake.

Idempotency key:

```text
payment:{customer_id}:{source_bill_id}:{amount_cents}:{payment_date}
```

Enforce idempotency in local SQLite before the network call. A Twilio retry,
double “yes,” worker retry, or app restart must still produce one Nessie write.

If the network times out after sending the request, mark the request
`reconcile_required`, not `failed`, because Nessie may have accepted it.
Before retrying, query Nessie for a matching payment. Never blindly POST again.

Never log API keys, account balances, full phone numbers, or webhook bodies.
The audit log may contain request id, normalized payee, amount cents, payment
date, actor role, transition, timestamp, idempotency key hash, and Nessie
payment id.

## WhatsApp and proactive delivery

Use the existing Twilio WhatsApp sender for both Margaret and Sarah. Add:

```text
SARAH_WHATSAPP_TO
TWILIO_OUTREACH_CONTENT_SID
TWILIO_APPROVAL_CONTENT_SID
```

Do not require a second Twilio number.

WhatsApp free-form messages are allowed only inside an open 24-hour user
session. Proactive outreach outside that window must use an approved content
template. The existing generic content SID must not be assumed to support bill
variables.

Create or configure templates whose approved variables cover only:

- Margaret outreach: payee, amount, due date
- Sarah approval: payee, amount, proposed payment date, reason

The first template message may be concise. Once Margaret replies, send the
full recommendation and read-back as free-form messages in the open session.
If template approval is unavailable before judging, have both demo phones send
one message to the sandbox within 24 hours and label that as a demo constraint.
Do not claim unrestricted production outreach.

Route inbound replies by exact configured phone number:

- `MOM_WHATSAPP_TO` -> Margaret role
- `SARAH_WHATSAPP_TO` -> Sarah role
- unknown sender -> reject or ignore

Continue validating `X-Twilio-Signature` before reading message content. Keep
returning empty TwiML immediately and process in the worker.

## Grounded wording

Construct a fact sheet in code:

```json
{
  "request_id": "short-safe-token",
  "payee": "Austin Energy",
  "amount": "$120.00",
  "due_date": "Oct 31",
  "proposed_date": "Oct 31",
  "balance_after": "$1,927.75",
  "buffer": "$200.00",
  "gate": "allow",
  "gate_reason": "known_payee_under_threshold"
}
```

Use deterministic fallback text for outreach, read-back, gate messages,
exceptions, and receipts. The LLM may make the sentence warmer, but reject its
output if it introduces a dollar amount, date, weekday, payee, or status absent
from the fact sheet. On model error or rejection, send the fallback.

Never say “paid” when Nessie only says `pending` or `scheduled`. Mirror the
actual returned status:

- pending/scheduled -> “scheduled”
- completed -> “paid”
- failed/unknown -> “I could not confirm it was scheduled”

## Endpoints

```text
POST /outreach/run
  Run one scheduler pass. Demo-only/manual trigger.

GET /payments/{request_id}
  Sanitized state for the demo panel. Do not expose the balance to Sarah.

POST /payments/{request_id}/cancel
  Cancel only when Nessie confirms cancellation is supported and the payment
  date has not arrived. This endpoint is not a substitute for WhatsApp undo.

GET /payments/audit
  Sanitized audit events for the demo panel.
```

The existing inbound WhatsApp endpoint handles Margaret's YES/NO/UNDO and
Sarah's APPROVE/DENY by role and active request.

## Tests that define the feature

1. **Safe known bill**
   - Austin Energy, $120, known payee, due Oct 31.
   - Planner chooses an on-time date leaving more than $200.
   - First Margaret yes advances to read-back, but creates no Nessie write.
   - Second Margaret yes authorizes and creates exactly one write.
   - Receipt mirrors Nessie's returned status.

2. **Exact buffer boundary**
   - A candidate leaves exactly $200.
   - It is unsafe.

3. **Deposit after due date**
   - Deposit arrives Friday after a Monday due date.
   - Planner never proposes Friday.
   - If no earlier safe date exists, result is `exception`.

4. **Large bill**
   - Amount $600.
   - Margaret confirms.
   - No Nessie write.
   - Sarah receives only payee, amount, date, and reason.
   - Sarah approves -> one write.
   - Sarah denies -> no write; Margaret receives the hold message.

5. **New payee**
   - Amount below $500 but payee absent from completed bill history.
   - Requires Sarah.

6. **Replay safety**
   - Deliver Margaret's confirmation twice.
   - Deliver Sarah's approval twice.
   - Restart the repository between retries.
   - Exactly one Nessie write.

7. **Changed bill**
   - Amount changes after read-back and before execution.
   - Old yes cannot authorize the new amount.
   - No write; send a fresh proposal.

8. **Ambiguous reply**
   - Two open requests and a bare “yes.”
   - No write; ask which bill.

9. **Expired confirmation**
   - Reply after 15 minutes.
   - No write; state is `expired`.

10. **Transport uncertainty**
    - Nessie times out after request transmission.
    - Do not POST again until reconciliation checks for an existing match.

11. **Outreach dedupe**
    - Run the scheduler twice with unchanged data.
    - One outreach only.

12. **Security regression**
    - Invalid Twilio signature -> 403.
    - Unknown sender cannot approve.
    - Existing question-answer tests continue to pass.

## Demo trigger and reset

The demo must not depend on wall-clock luck.

- `POST /outreach/run` invokes the real scheduler path against Nessie.
- The UI shows: bill discovered -> date planned -> message sent -> Mom
  confirmed -> Nessie write -> scheduled.
- A reset command clears only local demo authorization/audit state and restores
  documented sandbox fixtures through the existing explicit setup path.
- Never hide a canned payment behind the button. The live Nessie id returned by
  execution must appear in the demo panel.

## Done when

- A manual scheduler run initiates WhatsApp outreach without Margaret asking.
- The proposed date is always on time and leaves strictly more than $200.
- Margaret sees a recommendation, then an exact read-back.
- A known bill at or below $500 executes only after the second confirmation.
- A new payee or amount over $500 waits for Sarah.
- Sarah never sees the balance.
- Duplicate messages and retries cannot create a duplicate payment.
- The receipt uses the real Nessie response and accurate status language.
- Undo works only if Nessie supports it and the payment is still cancellable.
- Every state change has an audit event.
- Existing tests and the new payment tests pass with no live network access.

## Do not

- Do not build memory-driven monitoring in this task.
- Do not let one casual “yes” immediately move money.
- Do not suggest a date after the due date.
- Do not bypass Sarah for a $600 bill.
- Do not let the LLM perform arithmetic, approve, or call Nessie writes.
- Do not claim “paid” for a merely scheduled record.
- Do not retry an uncertain write blindly.
- Do not buy or require a second Twilio number for the demo.
- Do not log secrets, full phone numbers, full webhook bodies, or Sarah-visible
  account balances.
