# Run the automatic bill-payment rehearsal

This implements **bill monitoring → read-back → approval → schedule → automatic
simulated settlement → confirmation**. It is a separate demo runner; Rishy's
read-only WhatsApp app, Nessie reader, and money-contract stubs are unchanged.

The bank is a **local synthetic ledger**, not the live Nessie API. The optional
WhatsApp channel is real. Every payment proposal and receipt is labeled
simulated, and no code in this runner can send money or modify a real Nessie
customer. No OpenAI or Nessie key is needed for the local rehearsal.

## Start locally

From the repository root:

```sh
source .venv/bin/activate
# If not installed yet: python -m pip install -r requirements.txt
python -m scripts.autopay_demo
```

Open `http://127.0.0.1:5010` and paste the **demo access token printed in the
terminal** into the page. The page stores it in this tab's session storage;
the token is not embedded in HTML or URLs. All `/api/` controls require it.

The default story is deliberately short:

| Demo date | Event |
| --- | --- |
| October 24, 2026 | $300 available; $120 Austin Energy bill due in three days; $200 protected buffer |
| October 24 | Steady initiates a full read-back proposing October 26, after expected income |
| User clicks **Yes** | That exact payee, amount, and date are approved; payment is scheduled |
| October 26 | $1,000 fictional income settles; code rechecks funds and records one $120 payment |
| October 26 | Senior receives confirmation; simulated balance is $1,180 |

Click **Yes**, then **+1 day** twice to see it. Alternatively click **Auto-play**:
one simulated day passes every 20 real seconds. A background worker checks
bills and schedules once a second, even when the page is closed. The app must
remain running. **Pause dates** pauses the calendar, not monitoring.

Useful alternate runs:

```sh
# A five-second day, useful for a fast rehearsal
python -m scripts.autopay_demo --seconds-per-day 5

# Rebase the entire default story to your demo's date
python -m scripts.autopay_demo --as-of 2026-10-25 --db work/oct25.sqlite3

# New payee requires both senior and family approval
python -m scripts.autopay_demo --scenario family_approves --db work/family.sqlite3

# Bill is affordable only after income; date must reach its three-day window
python -m scripts.autopay_demo --scenario wait_for_income --db work/timing.sqlite3

# Cannot afford the bill; advance two days to reach its three-day window
python -m scripts.autopay_demo --scenario insufficient_funds --db work/insufficient.sqlite3
```

Some data scenarios are acceptance inputs for features beyond this runner, such
as subscription detection; selecting them does not implement those features.

## Rehearse the failure paths

- **Cancel before execution:** approve, advance one day, then click
  **Cancel payment**. Advancing again credits the fictional income but does
  not pay the cancelled request. Cancellation on or after the payment date
  cannot fabricate a refund.
- **Late income:** approve, click **Delay income**, then advance two days. The
  final settled-balance check puts the request on hold. It is never paid using
  a forecasted deposit that has not actually settled in the simulated ledger.
- **No reply:** advance past the consent expiry. The request is held and no
  payment is created. Family silence is also never approval.
- **Family decision:** use the family scenario above. Senior approval creates a
  flagged-item request on the family phone; approval schedules it, denial stops
  it. No balance is included in family messages.
- **Duplicate/restart:** repeat an approval or restart the runner with the same
  database. The schedule, message deduplication, receipt, and balance persist.
- **Reset:** restores the fictional bank, clock, approvals, and conversation;
  pauses auto-play; starts a fresh run. Old inbound message ids remain
  deduplicated so Twilio retries cannot authorize the new run.

## Connect to a real WhatsApp phone

This is an **opt-in messaging mode**, selected with `--whatsapp`. Running tests
or starting the default runner sends no external messages. Twilio credentials
and authorized test phones are configured locally, never committed.

In `.env`, configure:

```dotenv
TWILIO_ACCOUNT_SID=<your account SID>
TWILIO_AUTH_TOKEN=<your auth token>
TWILIO_WHATSAPP_FROM=whatsapp:+<your Twilio WhatsApp sender>
MOM_WHATSAPP_TO=whatsapp:+<your authorized test phone>
PUBLIC_BASE_URL=https://<your-tunnel-host>

# Optional: another authorized test phone, different from the senior
STEADY_TRUSTED_WHATSAPP_TO=whatsapp:+<trusted-person-test-phone>

# Optional: approved no-variable utility template inviting a reply
# Example template wording: "There is an update from Steady. Reply HI to view it."
# STEADY_WAKE_TEMPLATE_SID=HX...
```

Both phones must be allowed to message the configured WhatsApp sender. For
Twilio's sandbox they must join it first. Follow Twilio's
[sandbox instructions](https://www.twilio.com/docs/whatsapp/sandbox).

1. Run an HTTPS tunnel to port **5010**, for example:

   ```sh
   cloudflared tunnel --url http://127.0.0.1:5010
   ```

2. Set `PUBLIC_BASE_URL` to that tunnel's exact HTTPS origin, then start:

   ```sh
   python -m scripts.autopay_demo --whatsapp --db work/whatsapp-demo.sqlite3
   ```

3. In Twilio's WhatsApp configuration, set the incoming webhook to
   `https://<your-tunnel-host>/whatsapp/inbound`, method **POST**. Optional status
   webhook: `https://<your-tunnel-host>/whatsapp/status`, also POST.
4. If no approved wake-up template is configured, text **HI** from the senior's
   phone. This opens the WhatsApp session, and Steady sends the queued read-back.
5. Reply **YES ABCD1234**, using the actual reference in that message. This is
   one reply approving one exact proposal. **CANCEL ABCD1234** cancels before
   the payment date. Family replies use **APPROVE ABCD1234** or **DENY ABCD1234**.
6. Advance dates from the operator screen or turn on auto-play. The senior
   receives the simulated-payment confirmation on the real phone.

The local Yes button attaches the reference automatically. Plain WhatsApp
`yes` without a reference deliberately re-asks, because stale or ambiguous
replies must not authorize a different payment. A WhatsApp approval is accepted
only after the complete read-back has been sent to that phone. A template
wake-up alone does not count as a delivered read-back or as consent.

WhatsApp requires an approved template outside the 24-hour customer-service
window; a user message opens that window. This bridge sends free-form content
only inside an open session. If `STEADY_WAKE_TEMPLATE_SID` is configured, it
sends at most one generic invitation per role/run and queues the real bill
details until a reply. Do not point this setting at the old appointment
template or a template with unfilled variables. See Twilio's
[messaging-window rules](https://www.twilio.com/docs/whatsapp/key-concepts).

Incoming requests are verified with Twilio's SDK using the exact configured
public URL and auth token, following its
[webhook security guidance](https://www.twilio.com/docs/usage/webhooks/webhooks-security).
Only the configured senior/family sender numbers are accepted; the original
app's `ALLOW_ANY_SENDER` setting is not used. The operator UI cannot impersonate
phone approvals while in WhatsApp mode. The tunnel can expose the static demo
page, but bank state and controls still require the separate demo access token.

Outbound delivery states appear on the operator screen: `pending`, `sent`,
`awaiting_session`, `unconfigured`, `failed`, or `superseded`. `sent` means Twilio
accepted the send, not that the human read it. For `awaiting_session`, send HI
from that phone. After fixing a failed transport, a fresh HI retries queued
notifications; a network timeout can produce duplicate notifications, but the
same payment reference and durable ledger prevent duplicate simulated payments.

## State and safety boundaries

- SQLite stores state under `work/` (gitignored). Use a separate database for
  each scenario/date and for local versus WhatsApp mode. A restart resumes the
  stored run; changing CLI arguments does not silently replace its account.
- All ledger arithmetic uses integer cents. The $200 buffer permits equality.
  For this runner, an amount **at or above $500**, or a payee absent from the
  known list, conservatively requires family approval.
- Planning reserves other known pending obligations through the latest known
  deadline, capped at 90 days, and assumes no unlisted future spending. Income
  may support a forecast on its expected date; the executor uses settled cash.
- One proposal is active at a time. A reference fixes its bill id, payee,
  amount, date, and approvals. Changed facts require a new proposal; this demo
  holds the old request rather than silently changing an approved action.
- Pending consent is valid through the next simulated date or the due date,
  whichever comes first, and is held after that inclusive date. Approved schedules do not expire while waiting for their agreed
  execution date. Past-due or unsafe actions are held instead of paid late.
- Ledger debit, bill completion, receipt, audit event, and notification outbox
  update commit atomically. A run+bill key and serialized SQLite transaction
  prevent duplicate payment across retries, workers, and process restarts.
- The virtual clock pauses while the server is stopped. There is no real-time
  banking scheduler, external transfer, or production fraud classifier here.
- Keyword checks re-ask on ambiguous replies and warn on gift-card/crypto
  demands; the runner does not claim to implement the full scam/memory roles.
- Person 1 can use this as executable reference behavior for a future Nessie
  executor. A remote banking request cannot simply replace a local debit while
  retaining the same atomicity guarantee: it needs provider-specific identity,
  reconciliation of uncertain outcomes, and live sandbox schema tests.

## Verification

```sh
python -m pytest
```

Tests cover the complete synthetic settlement flow, cash reservations, exact
cent boundaries, missing amounts, changed facts, duplicate/concurrent replies,
transaction rollback, restart/reset, cancellation, family denial and timeout,
signed webhook routing, delivered read-backs, sender restrictions, notification
failures, and background-clock execution. No real messages or Nessie writes
are made by tests.
