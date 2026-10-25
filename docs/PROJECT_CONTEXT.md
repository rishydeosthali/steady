# Steady — project brief

## Goal and audience

Build a compelling HackTX demo for Capital One with Andrew and two teammates.
Steady is a proactive financial agent for older adults on fixed incomes. It
contacts them by phone and text before bills are due, plans payments around
income, and executes an agreed payment after a clear confirmation. The senior
does not need an app, login, or menu. Capital One's Nessie API is the confirmed
banking sandbox for this project.

Older adults face late fees, overdrafts, scams, and tools that require them to
open an app and interpret their finances. Steady monitors and plans first, then
asks for a simple decision.

## Intended capabilities

1. **Proactive outreach:** initiate voice calls or SMS about upcoming bills.
   Example: a $600 electric bill is due Monday; paying now leaves $100 until
   Friday, so suggest Friday after the expected deposit.
2. **Payment execution:** after explicit approval, schedule and execute through
   Nessie, send confirmation, and retain an audit trail.
3. **Cash-flow planning:** forecast deposits, bills, and spending in deterministic
   code; choose an on-time payment date that preserves a protected cash buffer.
   Handle situations with no safe date explicitly.
4. **Memory-driven monitoring:** remember patterns, preferences, and decisions;
   flag above-normal spending, purchases that jeopardize upcoming bills, and
   unusual recurring charges or subscription creep.
5. **Scam and anomaly guard:** detect new payees, urgency, and large or unusual
   transfers; pause suspicious actions and involve the trusted person.
6. **Trusted-person loop:** request approve/deny decisions by text for large or
   flagged payments while preserving the senior's independence.
7. **Forgiving conversation:** read back payee, amount, and date; clarify uncertain
   speech, allow pauses and rambling, and support undo/cancel before execution.
8. **Stretch:** accept a texted photo of a paper bill and extract payee, amount,
   and due date for confirmation.

The example dates and amounts are demo inputs, not validated financial advice
or assumptions about current Nessie API capabilities.

## Intended architecture

| Component | Responsibility |
| --- | --- |
| Monitor | Watch banking data for bills and anomalies |
| Memory | Store long-term patterns, preferences, and past decisions |
| Planner | Compute deterministic cash-flow forecasts and payment timing |
| Safety | Enforce amount, payee, timing, balance, and approval rules |
| Executor | Perform authorized Nessie writes with idempotency |
| Communications | Initiate phone/SMS contact and explain decisions plainly |

**The LLM handles judgment and language; code handles money.** These are logical
responsibilities, not a requirement to deploy six separate services or LLMs.
The application stack and division of implementation roles are still open.

## Three-minute demo target

1. Steady calls about the Monday bill and recommends a cash-flow-safe date.
2. The senior says yes; the agreed payment is scheduled through Nessie.
3. A memory-based warning explains that a purchase would jeopardize Thursday's bill.
4. A suspicious payee is blocked and the trusted family member receives a text.

Show real integration behavior where available and label simulations honestly.

## Positioning

Frame Steady as going beyond autopay and alerts to a proactive, reasoning agent.
The supplied brief mentions Capital One Eno, alerts, bill pay, and autopay;
competitor capabilities and claims of novelty remain unverified. Verify before
making comparative claims. Intended differentiators are outbound calls,
income-aware timing, memory-based warnings, and a family approval loop for seniors.

Suggested pitch from the brief:

> Capital One's tools wait for you to act. Steady acts for you, and checks with
> you only when it matters.

Treat the comparison in this pitch as messaging to validate before presenting.

## Starting state

The remote repository had no commits or branches when cloned on October 24,
2026. Setup creates Andrew's `andrew` branch with project context and agent
guidance. `main` must exist with shared history before opening a PR to it.
No application functionality was implemented by the initial setup task.

## Current implementation and team handoff

Andrew's synthetic-data work integrates Rishy's `b63af81` commit: a Python/Flask
WhatsApp webhook, Twilio outbound replies, OpenAI intent/wording, and a read-only
Nessie client. Payment functions in `backend/money/contracts.py` remain stubs.
The remote still had no `main` when this work fetched it; `rishy` was merged into
`andrew` instead. The current runtime uses Twilio WhatsApp; simulator-only
channel statements in `FEATURES_AND_GOALS.md` describe an earlier proposed scope.

Andrew supplied four work areas: (1) deterministic planning, safety, and payment
execution; (2) memory, monitoring, proactive triggers, and demo reset;
(3) texting, forgiving conversation, cancellation language, and stretch photo
intake; (4) trusted-person approvals, demo visibility, and pitch. These are work
areas, not a change to the number of teammates or assigned individual owners.

See `SYNTHETIC_DATA.md` for four fictional profiles, fifteen scenario expectations,
and integration commands. Fixtures support the current account-question flow;
their future money-action expectations do not imply those features are built.
