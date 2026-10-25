# Steady — features and goals

Steady is a text concierge for an older adult on a fixed income. It finds
upcoming bills, proposes a payment date that keeps a cash buffer, and pays
only after an explicit yes. A trusted person co-signs anything new or large.
Forwarded scam texts are checked against the real bill record.

This build is text inside the demo. Mom and Sarah read and reply in a
two-phone simulator. There is no phone call, SMS gateway, or Twilio channel.

**The LLM handles language. Code handles money.** One agent loop parses text.
Deterministic modules decide dates, gates, payments, and scam verdicts.

## Goal

Ship a two-minute HackTX demo in which judges watch three things happen for
real against Capital One's Nessie sandbox:

1. Steady proposes a safe date for a known bill, Mom says yes, and the bill
   appears in Nessie.
2. A new, over-threshold payee stops for Sarah, she denies it, and Mom gets a
   kind hold message.
3. A judge pastes a fake shutoff text. Steady debunks it with Mom's real
   payment record and notifies Sarah.

Closing line: "Steady pays the bills that are real, and proves the ones that
aren't."

## Demo data

| Item | Value |
| --- | --- |
| Customer | Margaret (Mom) |
| Trusted person | Sarah |
| Known payee used in the script | Austin Energy |
| Other known payees | Two more, created with the Nessie bills and named in setup |
| Co-sign threshold | $500 |
| Protected buffer | $200 |
| Income | Recurring Social Security deposit into checking |

Austin Energy is the local bill judges should recognize. The other two known
payees exist so "known payee" is a real list, not a single hard-coded name.

## Contracts

Lock these before feature work. The money layer has no LLM.

### Agent to backend

| Call | Result |
| --- | --- |
| Get upcoming bills | Bills due inside the scheduler window |
| Propose date | Earliest safe date, or `exception` |
| Request payment | `allow`, `needs Sarah`, or `blocked`, each with a plain-English reason |
| Execute | Creates the Nessie bill payment |
| Cancel | Cancels that payment before the payment date |
| Verify claim | Real bill status, last payment, and next due date for a claimed payee |

### Simulator to agent

| Call | Result |
| --- | --- |
| Send message | Accepts a text as Mom, Sarah, or the scammer box |
| Live feed | Outgoing texts for Mom's phone and Sarah's phone, as they are produced |

## Money and rules

This layer owns every Nessie read and write.

### Nessie setup

About ten API calls, done as setup, not a hidden seeder:

- One customer, Margaret, with a checking account.
- A recurring Social Security deposit.
- Three bills from known payees, including Austin Energy.

Confirm in Swagger before building undo: bill fields are payee, amount,
payment date, and status, and a bill can be cancelled or deleted before the
payment date. Undo depends on that.

### Date picker

Choose the earliest date on or before the due date where the balance after the
payment stays above the $200 buffer. If no such date exists, return
`exception`. The agent then texts both Mom and Sarah. The rule is one
deterministic sentence, not a forecast.

### Payment gate

| Situation | Decision |
| --- | --- |
| Known payee and amount under $500 | `allow` |
| New payee, or amount over $500 | `needs Sarah` |
| A rule blocks the payment outright | `blocked` |

Every decision includes a plain-English reason. Sarah's co-sign request
includes only the flagged item. It does not include the account balance.

### Executor

Creates or cancels the Nessie bill payment. Idempotency keys make a retry
unable to pay twice. Every action is logged.

### Scheduler

Finds bills due within N days and starts outreach. A manual "fire now" trigger
starts the same path for the demo.

### Scam-check facts

Given a claimed payee, return the real bill status, last payment, and next due
date from Nessie. The money layer returns facts. It does not write the reply.

## Agent and conversation

This layer owns every sentence Margaret and Sarah read. Unclear input gets a
re-ask. The agent never guesses an intent.

### Intent parsing

Mom's text becomes one of: `yes`, `no`, `undo`, `question`, or `forwarded
message`.

### Concierge flow

1. Outreach text for an upcoming bill.
2. Date proposal with a one-line reason.
3. Read-back of payee, amount, and date before any payment.
4. Confirmation after execute.
5. Undo available before the payment date.

If the date picker returns `exception`, both phones get a text.

### Sarah flow

1. Co-sign request for the flagged item only.
2. Her reply is approve or deny.
3. Mom receives a message after a denial.

Open decision, owned here: whether Mom's denial text says Sarah said no, or
says the payment is on hold. Pick one before the gate beat is rehearsed.

### Scam-check extraction and reply

The LLM pulls the claimed payee, amount, payment method, and urgency from the
forwarded text. Code then applies fixed verdicts:

| Facts | Verdict |
| --- | --- |
| Payee is known, and the bill is already paid or not due | Scam. The reply cites Mom's real payment record. |
| The text demands a gift card, crypto, or a wire | Scam. |
| Payee is unknown | Steady cannot verify it, tells Mom not to pay, and asks Sarah. |

### Tone

Short sentences. No jargon. One decision per text.

## Demo surface

Presentation is the bottleneck, so this surface is part of the product.

- Mom's phone and Sarah's phone, side by side.
- A scammer input box the judge types into.
- A live Nessie panel that shows bills appearing, cancelling, and completing.
  That panel is how judges see the API is load-bearing.
- A reset button that returns all demo data to a clean state between visits.

## Pitch

- **Hook:** the FBI elder-fraud figure from the latest ic3.gov report.
- **Differentiation:** "Carefull and EverSafe watch; Steady does the chore."
- **Demo:** the three-beat script below.
- **Architecture:** one slide. The LLM handles language. Code handles money.
  The six-way split (monitor, memory, planner, safety, executor,
  communications) is that slide, not six agents in the build.
- **Close:** "Steady pays the bills that are real, and proves the ones that
  aren't."
- **Q&A:** seniors trust it because money moves only after a read-back, only
  through the bank, and only inside the never-rules below. Sarah has the veto
  because Mom consented at signup. A hijacked "yes" still cannot pay a new
  payee or an amount over $500.

### Never-rules

- Never guess an unclear text.
- Never let the LLM create, cancel, or approve a payment.
- Never pay a new payee or an amount over $500 without Sarah.
- Never show Sarah the balance.
- Never pay twice.
- Never skip the read-back.

## Demo script

About two minutes.

1. **Concierge.** Steady texts about the Austin Energy bill and suggests Friday
   after the deposit. Mom replies "yes." Steady reads the payment back. The
   Nessie bill appears on screen.
2. **Gate.** Mom asks Steady to pay a new roofer $2,400. Sarah's phone lights
   up. Sarah denies. Mom gets a kind hold message.
3. **The trick.** A judge types a fake "Austin Energy shutoff, pay now" text
   into the scammer box. Mom forwards it. Steady debunks it with her real
   payment record. Sarah is notified.

## Build order

| When | Done means |
| --- | --- |
| Hour 0 (30 min) | The contracts, demo data, threshold, and buffer in this file are locked. |
| Hour 3 | Beat 1 works end to end in the simulator. If it does not, cut scope immediately. |
| Hour 5 | All three beats work. Feature freeze. |
| After hour 5 | Record a full backup video of the three beats, then rehearse to time at least three times. Two people play Mom and Sarah. |

## Out of scope for this build

- Real SMS, a verified sender number, and Twilio.
- Voice calls.
- A second agent loop, a photo-of-a-bill intake, or a spending forecast.
- Memory of past purchases beyond the bill, payee, and payment facts Nessie
  already stores.
