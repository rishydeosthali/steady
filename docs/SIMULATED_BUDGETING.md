# Named savings goals and budgeting actions

This feature is available only in the hosted/local **simulation**. It moves
fictional checking money into named savings pots; it never writes to Nessie or
transfers real funds. Existing bill payments still need both approvals.

## Set a plan through Slack or WhatsApp

Send this fictional example to Steady:

> Budget plan: save $22,000 for car, allocate 10% of each new deposit to car;
> save $1,000 for emergency, allocate 5% to emergency; cap vacation at $200
> monthly; keep $200 cushion and $100 weekly essentials.

Steady reads back the policy. `CONFIRM PLAN <reference>` authorizes its bounded
simulated allocations. A draft alone does not move money, and a bare YES does
not confirm a policy. Up to four distinct goals can share at most 100% of new
received income. Names, targets, percentages, caps and reserves are grounded in
the message and validated in code. A named goal does not need a deadline: the
car target can remain a long-term objective until its pot is fully funded.

`PAUSE PLAN <reference>` stops new allocations; `RESUME PLAN <reference>` resumes
them. Existing pot balances, cushion and essentials protections stay intact.
Ask "How is my budget plan?" for progress. Confirming a replacement plan keeps
existing pots but excludes all income already completed at that confirmation,
so changing a rule cannot allocate an old deposit again.

## Agent behavior

The controller observes new income, spending, bills and pot balances. Its tools
include `review_spending` and `allocate_savings`. When confirmed goals have
eligible income, its structured tool choices prioritize that actionable work
before ordinary bill inspection/waiting. The model chooses which goal to work
on and sees the actual result before deciding again. The server determines the
amount, destination and eligibility; the model cannot supply arbitrary money
arguments, change a policy or withdraw savings.

For a $2,500 received deposit, the example policy can allocate $250 to car and
$125 to emergency. The transfer and its receipt are committed atomically in
the same SQLite account record. Checking falls by exactly the amount each pot
gains. Pending income and completed income from before confirmation are not
eligible. Cents are rounded down, targets cannot be overfunded, and per-source
credits prevent duplicate allocations. A funded pot completes its allocation
goal; category monitoring continues.

Before allocating, code reserves every known unpaid bill, the protected cash
cushion, and 30 days of essentials (using the stricter existing budget allowance
when present). Uncertain/reconciling payments block allocations. Unsafe amounts
remain pending for a later observation and can be retried after recovery.

Category caps cover completed categorized purchases in the current calendar
month. Reports show remaining allowance and recommend pausing further category
spending when over cap. These are **advisory controls**: posted purchases remain
facts even if they exceed the cap; Steady does not pretend to reverse them or
block a real card. Suggested restraint is not counted as money actually saved.

## Scenario studio

The **Savings & spending** view shows each goal's actual simulated balance,
remaining target, percentage, category allowances and allocation receipts.
It also accepts new fictional income, dated either received or expected.
The expense form supports explicit categories; unknown expenses default to
Other rather than being guessed into a category.

New quick scenarios:

- **A new payday:** receive $2,500 fictional income.
- **A busy spending week:** atomically post groceries $80, transport $30,
  dining $55 and a vacation deposit $120. Retrying cannot post them twice.
- **Vacation spending grows:** post a $350 vacation expense to test cap advice.

The background goal worker acts after account changes; repeatedly asking for
calculations is unnecessary. Free Render still sleeps when idle and loses local
state across redeploys. Saved scenario loads require a new policy confirmation;
they preserve opening pots but never carry active allocation authority forward.

## Verification

`tests/test_budget_actions.py` covers multiple targets, new-income-only funding,
conservation of cents, repeated source events, pending/paused rules, budget caps,
unsafe allocation deferral and recovery, invalid destinations, goal completion,
restart, batch rollback, and three approved simulated bill payments alongside
savings allocations. Existing signed Slack approval and cancellation tests run
unchanged. Real-model tests additionally verify actual selection of the new
allocation tools; no external messages are sent in those local rehearsals.

Live Slack rehearsal on November 12, 2026 confirmed a $22,000 car/10% policy
and $1,000 emergency/5% policy. A $2,500 deposit produced $250/$125 allocations;
a second $500 deposit independently increased the pots to $300/$150, with four
source-bound receipts. Busy-week and vacation scenarios recorded $470 against
the $200 vacation cap, and the agent proactively recommended pausing further
vacation spending. These were fictional account changes, not real transfers.
