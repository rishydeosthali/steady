# Agent continuity verification

## Slack continuation

- Signed Slack DMs successfully created and confirmed fictional savings goal
  F8FA1560 ($300 by December 25, $200 cushion, $100 weekly essentials).
- Before the coverage fix, confirmation produced no budget report. A simulated
  $1,900 expense independently held an unsafe bill and generated a fresh dated
  proposal, but savings reporting was still missing.
- The real model reproduced goal starvation: six bill inspection/reconsideration
  calls exhausted the turn without calculating the budget. Bill reconsideration
  was being attempted even for invoices not held for funds.
- The coverage fix supplies eligible invoice IDs, prioritizes unreported budgets,
  blocks ineligible reconsideration, and guarantees a deterministic budget report
  after each successful bounded turn. Existing outbox deduplication prevents
  repeated unchanged reports. No goal values or approvals change automatically.
- Real-model retest with fictional data selected report_budget first and sent a
  grounded forecast, then inspected bills and waited after an ineligible action.
  Messaging was faked for that diagnostic; this is separate from live Slack proof.

## Before changes (November 12, 2026)

- WhatsApp: asking to resume the previously discussed goal and recall its target
  returned an unrelated existing bill-increase alert. Goal recall and next-step
  selection failed. No payment was requested during this test.
- Render: `STEADY_AUTONOMY` was `0`; the deployed commit was `5410adb`.
- Code review: `backend.hosted` starts the simulation and recap workers but not
  `GoalWorker`, so enabling the flag alone does not enable background goal work.
- Code review: goal intake requires the target and deadline together, does not
  persist incomplete intake, and ignores numeric follow-ups without goal keywords.
- Code review: the default goal database is outside the simulation data directory.
- Earlier user-provided transcript shows a payment completion preceding its
  approval notification. Delivery order alone does not prove payment before approval;
  notification retry ordering needs a regression test.

- WhatsApp fictional-goal test: a complete synthetic target, deadline, cushion,
  and essentials allowance also returned the same unrelated alert.
- Render lists the service as Free. That plan cannot guarantee continuous
  background monitoring or durable local SQLite files across redeployments.

Local regression tests use dummy services and move no real money. The patch
adds multi-turn intake and restart coverage, hosted worker lifecycle coverage,
simulation goal database placement, and notification ordering after send failure.

Local validation: `python -m pytest -q` passed all 434 tests. Synthetic fixture
validation passed for four fictional profiles and fifteen scenarios.

## Live retest

- Commit `3700323` deployed successfully, and `STEADY_AUTONOMY` was enabled.
- A partial fictional goal ending in "$300." reached goal intake instead of
  repeating the alert, but returned the generic validation fallback. A regression
  test reproduced the pre-existing punctuation boundary bug. The follow-up patch
  allows sentence punctuation while still rejecting partial decimal matches.
- Real-model extraction of the complete fictional goal produced the exact date
  phrase "by December 25, 2026". The date parser rejected the leading "by".
  The follow-up patch normalizes "by"/"on" after checking the phrase against
  the user's text; a regression uses the observed synthetic model output.
- Free-plan redeployments restarted the fictional account and generated new bill
  proposals. This reinforces the need for persistent storage before relying on
  durable unattended monitoring.
