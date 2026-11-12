# Persistent budgeting and autonomous bill resolution

Steady now has a bounded, model-directed controller around its existing tools.
It chooses its next action from facts and tool results rather than running one
fixed sequence. Python owns all arithmetic, permissions, payment state changes,
and receipt verification. This uses the existing Nessie sandbox and independent
simulated biller; it does not add real banking or transfer money into savings.

## Enable and run

Use the original `python -m backend.app` server. Add these local settings:

```dotenv
STEADY_AUTONOMY=1
STEADY_GOAL_DB=data/goals.sqlite
```

Existing Twilio, OpenAI and Nessie configuration is required. Budgeting works
without external-biller mode. For executable bill resolution, also configure
`STEADY_EXTERNAL_BILLER=1`, the independent biller, and the caregiver email
settings as described in `EXTERNAL_BILLER.md`. Start `python -m biller_simulator`
and `python -m backend.app`. No credentials are generated or changed by this
feature. The standalone `scripts.autopay_demo` is unchanged.

The hosted `python -m backend.hosted` entry point also starts and stops the goal
worker when enabled. The Render blueprint enables `STEADY_AUTONOMY=1`; existing
services must update that setting separately. In simulation mode the default
goal database is `STEADY_SIMULATION_DIR/goals.sqlite`, alongside the account and
payment databases. Set `STEADY_GOAL_DB` only to override that path. A free Render
instance can sleep and its filesystem is ephemeral; continuous monitoring and
restart durability require an always-on instance with persistent storage.

Send from the configured senior's WhatsApp:

> Save $300 by December 25, 2026, keep a $200 cushion, and allow $100 weekly for essentials.

Use a future date within one year. Essentials means living costs outside listed
bills, such as groceries and transport. Christmas is also recognized. Steady
extracts values, checks they occur in the user's text, validates them in code,
and reads back the complete proposal with an eight-character reference.

Goals can be supplied over several messages: "My goal is to buy a car", then
"around 22 k", then a deadline and weekly essentials. Incomplete intake survives
restart and Steady asks for the next missing field. A short numeric/date answer
continues that intake; payment decisions still use the separate payment path.
Asking about an unconfirmed proposal repeats its target and confirmation code.

Reply `CONFIRM GOAL <reference>` to activate. A bare YES never changes a goal.
The confirmation replaces the previous budget only after explicit agreement;
goal proposals expire after 24 hours. Ask `How is my budget?` for a fresh report.
`PAUSE GOAL <reference>` stops budget pursuit and `RESUME GOAL <reference>` resumes
it. Pausing does not remove the confirmed cushion or living-cost protection
from payment checks. Bill approvals remain independent.

## Memory and execution

`backend/autonomy/memory.py` stores the following in the private, gitignored
SQLite database:

- Confirmed structured goals, proposals, deadlines, allowances and status.
- The latest budget calculations and twelve recent financial snapshots.
- Two hundred recent tool decisions, brief explanations, results and failures.
- Durable inbound deduplication, notification outbox and scheduled next check.
- One bill-resolution goal per discovered provider invoice, including a verified
  receipt only after the existing payment service has checked it.

An in-process lock and a ten-minute SQLite lease prevent simultaneous controller
runs and notification flushes from different processes. The application is for
one configured household; do not share a goal database across different users.

The worker observes every 60 seconds. It invokes the model when financial facts
or active goals change, or the stored follow-up becomes due. A run allows at most
six model-selected tools. Each result is included in the next model call, and
prior decisions are available on restart. Repeated identical actions stop the
run. Model/read failures preserve the goal and schedule a retry after ten minutes;
the controller never makes financial decisions using a failed refresh.

Available tools: refresh facts, calculate a budget, inspect an invoice, reconsider
a funds-held invoice, report the calculated budget, report an observed bill
status, and wait for a scheduled follow-up of 5 minutes to 24 hours. Reports use
deterministic wording. The model supplies a brief action explanation, not payment
arguments or unrestricted messages. There is no approve/execute-payment tool.

The background payment worker continues enforcing previously authorized jobs
and reconciling their receipts. The goal controller supervises and adapts plans;
it does not replace the payment state machine.

Every successful bounded turn covers each active savings goal with a deterministic
forecast and deduplicated report, even when the model spends its steps on bills
or chooses to wait. The model receives eligible funds-held invoice IDs; awaiting
approval, cancellation and ordinary unpaid invoices cannot be reconsidered as
funds-recovery jobs. This prevents routine bill inspection from starving savings.

## Budget meaning and boundaries

The savings target is an amount intended to remain available **above** the cash
cushion, after known obligations and the confirmed essentials allowance through
the deadline. The report is projected capacity, not a verified savings account
balance. `verified_saved_cents` remains unknown; no savings transfer is invented
and a feasible forecast never marks the goal achieved.

Calculations use integer cents, round daily living-cost reserves upward, count
completed transactions only through the current balance, and include future
listed pending deposits only as conditional forecast income. Same-day pending
income is not cleared cash. No recurring future income or bills are invented.
Missing bill dates or amounts make a report incomplete. Missing essentials
requires clarification. A missed deadline requests review instead of silently
changing the target or claiming success.

Essential bills take priority over savings. The confirmed cushion (minimum $200)
and daily essentials allowance also feed the payment planner and final execution
check. Payment planning reserves at least fourteen days of essentials, or longer
when its known-obligation horizon is longer. Changes to targets, allowances,
deadlines, or the cushion require another exact goal confirmation.

## Autonomous bill recovery example

1. A bill is approved for a date after an expected deposit.
2. The deposit fails to settle; fresh execution checks hold the payment.
3. The goal controller observes the held bill and can inspect the facts.
4. When funds recover, the model can choose to reconsider it.
5. Code checks the invoice, account, payee, amount, deadline and cash forecast.
6. A safe new plan gets a new reference and idempotency key, with both approvals
   cleared. The senior and caregiver must approve the new proposal.
7. The existing service submits once and reconciles the provider receipt. Only
   then does the resolution goal become complete.

Cancellation, denial, senior holds, expired consent, changed invoice identity,
unknown payees, paid bills and uncertain submissions cannot be reopened by this
tool. The agent cannot lower a cushion, extend a deadline, or reinterpret a
budget reply as payment approval. This first version investigates available
account/invoice data; it does not contact utilities, negotiate fees, cancel
subscriptions, or search arbitrary payment websites.

## Delivery and inspection

Goal reports remain queued outside WhatsApp's session window. Send HI to reopen
it. Failed sends remain retryable; an updated queued report supersedes an older
unsent report for that goal. Delivery is at least once: a crash after Twilio
accepts a message but before SQLite commits can duplicate a text, never consent
or a payment. Successful proposal sends are acknowledged before marking the
read-back delivered. Existing follow-ups also acknowledge only after sending.

Local-only routes, blocked through a forwarding proxy/tunnel:

| Route | Purpose |
| --- | --- |
| `GET /goals` | Goals, facts, tool results, reports and next check |
| `POST /goals/run` | Force one bounded reasoning turn and flush messages |

Financial memory stays out of standard application logs and Git. `/goals`
contains private financial data and is for authorized local operators.

## Verification

```sh
python -m pytest
python -m scripts.demo_data validate
```

`tests/test_autonomy.py` uses a scripted model and fake bank/WhatsApp transport;
payment tests call the independent biller over actual local HTTP. It covers
goal confirmation and restart, changing facts and adaptive tool use, forecast
math, incomplete data, missed deadlines, bounds and leases, queued delivery,
sender/route isolation, fresh approvals after recovery, prohibited retries,
budget protections at execution, and independently verified receipts.

Real model behavior and delivery through the configured phone still require a
manual rehearsal. Model-directed behavior is bounded and fallible; the existing
deterministic checks remain the authority for every money action.
