# Steady: agent instructions

Read `docs/PROJECT_CONTEXT.md` for the product brief and demo priorities. Andrew
is collaborating with two teammates for HackTX and will assign specific roles
in later prompts. Work on the assigned task; do not build unrelated features.

## Product principles

- Steady helps older adults manage bills by outbound phone calls and SMS.
- The LLM handles judgment and language; deterministic code handles money.
- Payment execution requires explicit confirmation tied to the payee, amount,
  and date; enforce cash buffers, safety checks, and idempotency in code.
- Use Capital One's Nessie API as the confirmed sandbox integration. Never silently substitute
  real banking or claim an unimplemented feature works.
- Use patient, plain language and support clarification, cancellation, and a
  trusted-person approval loop without taking away the senior's independence.
- Keep credentials and personal financial data out of Git and logs.

## Collaboration and Git

- Andrew's working branch is `andrew`; the intended shared integration branch
  is `main`. Inspect the current branch, status, and remote refs before changes.
- Keep implementation on the assigned working branch and integrate through PRs
  to `main`. Do not push directly to `main`, force-push shared branches, or
  discard another contributor's changes without explicit authorization.
- Fetch before integration. Prefer merging `origin/main` into the working
  branch to rewriting a published branch. If `main` does not exist, report it;
  do not invent a base or silently initialize the team's integration branch.
- When a requested task needs integration, resolve merge conflicts
  autonomously when intent is supported by the code, requirements, and tests.
  Read both sides and nearby code; preserve both contributors' intended behavior.
  Never blindly choose `ours` or `theirs`, delete tests to get a passing result,
  or overwrite unrelated uncommitted work.
- For incompatible product decisions or uncertain payment/security behavior,
  explain the exact competing intentions and ask for the missing decision.
  Continue independent work where possible.
- After conflict resolution, check for leftover conflict markers, review the
  diff, and run relevant checks. Regenerate generated files/lockfiles with the
  appropriate tooling instead of guessing their content.
- Enable `git config --local rerere.enabled true` in new clones so Git can
  remember resolutions. Keep `rerere.autoupdate false` so reused resolutions
  are reviewed before staging. These settings are local, not tracked by Git.
- Use separate worktrees for concurrent coding agents to avoid concurrent
  edits to the same checkout. Coordinate file ownership and interface changes.
- PR descriptions should explain the behavior change, validation, and any
  remaining limitations. Creating or merging a PR is not a substitute for
  resolving conflicts and checking the resulting code.

## Verification

The current app is Python/Flask with Twilio WhatsApp, OpenAI, and a read-only
Nessie client. Run `python -m pytest`; tests use fakes and need no credentials.
Inspect current files before assuming payment/monitoring features are built.
Synthetic data usage and its limits are in `docs/SYNTHETIC_DATA.md`; validate
the authored fixtures with `python -m scripts.demo_data validate`.
For money-related code, test deterministic forecasting, buffer enforcement,
approval state transitions, duplicate delivery/retries, and cancellation.
