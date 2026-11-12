# Hosted scenario studio

The original Flask WhatsApp agent now has an explicit `STEADY_SIMULATION=1`
mode. Its dashboard, agent answers, watcher, and payment planner read the same
persistent fictional account. All money is simulated. Twilio messages and
caregiver emails are real when configured. This mode makes no Nessie requests.

## Deploy through Render's website

1. Sync the reviewed changes to your GitHub working branch using your Git GUI.
   The Blueprint file is `render.yaml` in the Git repository root (the inner
   `steady` folder in this workspace). Do not upload `.env`, `data`, `work`, or
   `.venv`. Do not overwrite concurrent work or push directly to a shared main.
2. In Render, choose **New → Blueprint**, connect this repository, and choose
   the branch containing the changes. The blueprint creates one paid Starter
   web service and a 1 GB persistent disk. Review Render's current price before
   creating it. A sleeping free service is unsuitable for ongoing monitoring.
3. Fill the prompted environment fields in Render. Use your existing Twilio
   account SID/auth token, WhatsApp sender, authorized recipient, template SID,
   and OpenAI API key. Sender and recipient use the `whatsapp:+...` format.
   For caregiver approvals, supply `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, and
   `CAREGIVER_EMAIL`. Use the existing app password, not a Google login password.
   Keep all secrets in Render's Environment screen; never commit or paste them
   into chat. Optional `CAREGIVER_NAME` and `STEADY_CUSTOMER_NAME` customize names.
4. Leave `PUBLIC_BASE_URL` and `STEADY_APPROVAL_BASE_URL` unset. Render supplies
   `RENDER_EXTERNAL_URL`; the app uses that stable HTTPS origin for Twilio
   signature validation and caregiver links. Set both explicitly if using your
   own custom domain. Remove old tunnel URLs from hosted environment settings.
5. Once deployed, open Render's service URL. Copy its generated
   `STEADY_DASHBOARD_TOKEN` from Environment into the studio's access-key field.
   The access key is kept in page memory, not embedded in HTML or stored in a URL.
6. In Twilio's WhatsApp Sandbox settings, set **When a message comes in** to
   `https://YOUR-SERVICE.onrender.com/whatsapp/inbound`, method **POST**.
   Set the status callback to the same origin plus `/whatsapp/status`, **POST**.
   For a registered WhatsApp sender, update that sender's webhook instead.
7. Text **HI** from the authorized WhatsApp phone. In the studio, the connection
   changes to ready. Run **Charged twice** and verify both the dashboard alert
   and the message on the phone. Ask for your balance; it should reflect the
   extra $120 expense. Your computer and tunnel can now be off.

If you create a Web Service manually instead of a Blueprint, use Python,
build with `pip install -r requirements.txt`, start with
`python -m backend.hosted`, health path `/healthz`, and mount the persistent
disk at `/var/data/steady`. Copy the environment entries from `render.yaml`.
These are host configuration fields; no local terminal is needed.

## Use the studio

- **Expense:** merchant, exact dollar amount, and posted date. Reduces settled
  cash once, then runs watcher and payment checks. A small safe purchase may
  need no alert. A retry uses the same request identity to avoid a second debit.
- **Bill:** creates or updates an unpaid bill for the named payee. A changed
  invoice invalidates its old proposal. New or unusually large payees stay held.
- **Income:** delay a pending deposit, or explicitly mark it received. Only
  settled income increases available cash.
- **Advance 1 day:** moves the paused demo clock one day, credits due pending
  deposits once, then checks payments. The clock never advances on its own.
- **Quick starts:** add an event to the current run; they do not secretly reset
  the account. Reset first if you want to repeat an identical starting state.
- **Request fresh proposals:** rechecks funds-held invoices immediately; normal
  background checks also propose again when a safe date becomes available. Senior holds,
  denials, cancellations, and uncertain submissions are not revived. Every new
  proposal requires fresh senior and caregiver consent.
- **Save / Load:** saves a financial starting point. Loading creates new account
  and invoice identities without inheriting approvals or pending messages.
- **Reset:** replaces only this simulation's account and pending decisions.
  Saved scenarios and the audit history remain. Old caregiver links stop working.
  Reset is refused while an uncertain payment is being reconciled.

The Account & bills tab shows the shared account and simulated local receipts.
The Activity tab shows operator changes and agent decisions. The message panel
distinguishes waiting, failed, and sent messages. “Sent to Twilio” means accepted
by the messaging API, not proof that the phone displayed it.

## Runtime and boundaries

- `backend.hosted` uses Waitress with one process, eight request threads, and
  one background monitor. Checks continue every ten real seconds while the
  website is closed; the fictional date stays paused. No tunnel is involved.
- Keep **one service instance and one process**. An in-process lock serializes
  account changes, agent replies, and payment transactions. Scaling requires
  distributed coordination and a shared database rather than extra replicas.
- `STEADY_SIMULATION_DIR` stores account, payment, watcher, audit, and WhatsApp
  session data on the persistent disk. Redeploying preserves these records.
- The dashboard uses an operator bearer key; webhook routes still require
  Twilio signatures and the configured sender. The older local-only routes
  remain local-only. Caregiver links retain their per-request authorization.
- WhatsApp free-form messages require an inbound message in the past 24 real
  hours. Alerts wait durably while the window is closed and are retried after
  the next HI. Advancing simulation days does not advance this real-time window.
  Sandbox membership can also expire and require rejoining.
- No outbound messages are generated merely by the login page. The hosted
  monitor starts proposing eligible bills when the service starts; messages
  wait for the authorized phone's open session.
- Simulation mode uses a **local simulated biller in the same service**. It is
  not evidence of payment to an independent HTTP provider. The existing
  `STEADY_EXTERNAL_BILLER=1` mode remains available separately and cannot be
  enabled at the same time. The earlier autopay rehearsal is unchanged.
- The Render blueprint leaves the separate autonomy extension off. Its future
  budget/goal integration is separate from this deterministic scenario studio.
- Delivery is at least once. A crash around a notification can duplicate a
  message; durable biller receipts and payment keys prevent duplicate payment.

## Verification

`tests/test_simulation_studio.py` exercises remote operator authentication,
shared WhatsApp balances, exact-cent input, concurrent retries, posted-expense
accounting, alert delivery/queueing, approval invalidation, income settlement,
restart persistence, and reset identity. Tests use fakes and send no real
messages or emails. Run the repository's full pytest suite when validating a
release, including `tests/test_external_biller.py` for the existing HTTP mode.
