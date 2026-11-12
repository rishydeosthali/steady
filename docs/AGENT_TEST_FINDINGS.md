# Agent continuity verification

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
