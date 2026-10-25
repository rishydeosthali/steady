# Steady

A proactive financial agent for older adults, by phone and text. Steady watches
for upcoming bills, plans payment timing around income, and executes a payment
after explicit confirmation using Capital One's Nessie sandbox.

**The LLM handles judgment and language; code handles money.**

See [the project brief](docs/PROJECT_CONTEXT.md) for features, architecture, and
the HackTX demo, and [AGENTS.md](AGENTS.md) for collaboration and conflict handling.

## WhatsApp base agent

Margaret texts Steady's WhatsApp number. Twilio POSTs to the app, the app reads
her Nessie data, and Twilio sends the reply back to her WhatsApp. This slice is
read-only: it answers questions about her balance, bills, deposits and spending.
It never creates, cancels, or approves a payment. Asking it to pay gets "paying
is not turned on yet".

```
Twilio WhatsApp --POST--> /whatsapp/inbound  (signature checked, empty TwiML at once)
                                 |
                          worker thread
                                 |
     intent.py (OpenAI label) -> loop.py -> nessie/client.py (GET only)
                                 |
                          reply.py (OpenAI wording, checked against the facts)
                                 |
              twilio_client.py --Messages API--> Margaret's WhatsApp
```

Layout: `backend/app.py` (webhook), `backend/agent/{loop,intent,reply,facts,llm}.py`,
`backend/nessie/client.py`, `backend/twilio_client.py`. Later-slice function
boundaries (upcoming bills, propose date, request payment, execute, cancel,
verify claim) are stubs in `backend/money/contracts.py`. Nothing calls them.

### Setup

```sh
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # Windows: copy .env.example .env
```

Fill in `.env`. It is gitignored. Never commit it or paste its values anywhere.
The server stops at startup and names any missing variable:

`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`,
`MOM_WHATSAPP_TO`, `TWILIO_CONTENT_SID`, `NESSIE_API_KEY`, `NESSIE_BASE_URL`,
`OPENAI_API_KEY`. Optional: `OPENAI_MODEL` (default `gpt-4o-mini`),
`PUBLIC_BASE_URL`, `ALLOW_ANY_SENDER=1`, `STEADY_CUSTOMER_NAME` (default `Margaret`).

### Run the server

```sh
python -m backend.app             # listens on http://127.0.0.1:5000
```

### Open the tunnel

Twilio needs a public HTTPS URL. Any tunnel works. With Cloudflare (no account
needed for a quick tunnel):

```sh
cloudflared tunnel --url http://127.0.0.1:5000
```

or with ngrok: `ngrok http 5000`. Note the `https://<tunnel-host>` it prints.
A quick tunnel gets a new host every time it starts, so repeat the next step then.

### Point the webhook at the app

```sh
python -m backend.register_webhook https://<tunnel-host>
```

This prints the exact URL: `https://<tunnel-host>/whatsapp/inbound`, method POST.
If `TWILIO_WHATSAPP_FROM` is an incoming phone number on the account, it sets
`sms_url` through the Twilio API. The upgraded account uses the WhatsApp sandbox
number `+14155238886`, which the API cannot update. Set that webhook here:

> Twilio Console > Messaging > Try it out > Send a WhatsApp message > Sandbox settings
> - When a message comes in: `https://<tunnel-host>/whatsapp/inbound`, method POST
> - Status callback URL: `https://<tunnel-host>/whatsapp/status`, method POST

`/whatsapp/status` returns 204 and never replies.

### Open the thread

WhatsApp only allows free-form replies inside the 24 hour window that an inbound
message opens. To start a conversation with no recent inbound message, send the
approved template instead:

```sh
python -m backend.ping            # sends TWILIO_CONTENT_SID to MOM_WHATSAPP_TO
```

The app never sends a free-form message unless Margaret wrote first.

### How a text is handled

| Her text | Result |
| --- | --- |
| "What is my balance?" | Checking balance from Nessie |
| "What bills do I have?" | Payee, amount, payment date, status for each bill |
| Deposits or spending question | Reads deposits or purchases only for those questions |
| "Pay Austin Energy" | "Paying is not turned on yet." No payment is made |
| yes / no / undo / forwarded text | Recognised. Says these are not available yet |
| Gibberish or off-topic | Asks again. Nessie is not called |
| Invalid `X-Twilio-Signature` | 403 |

Every dollar amount and date in a reply must appear in what Nessie just returned.
If the model's wording contains anything else, the app sends a plain rendering
of the real facts instead. A fact Nessie did not return is stated as unknown.

If Nessie has no customer named Margaret, the app does not create one. It logs
the customer names that do exist and tells her it cannot see her account.
Nessie setup (customer, checking account, bills, deposit) is a separate step.

### Tests

```sh
python -m pytest
```

Tests use dummy credentials and fakes. They need no network and no `.env`.

## Recommended payment integration: external demo utility

The original WhatsApp backend can now connect to an **independent biller API**
with its own database and receipt page. Nessie supplies funds checks; a signed
phone approval authorizes an HTTP request that marks a fictional utility invoice
paid and returns a receipt. No real money moves and Nessie is not debited.

If your previous checkout tracked `.env`, **save your local copy before pulling**;
this change stops tracking that file. Restore it privately afterward.

```sh
python -m scripts.biller_demo init
python -m biller_simulator        # terminal 1: provider on :5011
python -m backend.app             # terminal 2: original Steady backend on :5000
```

Use the existing Twilio tunnel/webhook on port 5000. Open the provider page at
`http://127.0.0.1:5011` to verify its invoices and receipts independently.
See [Rishy's external-biller handoff](docs/EXTERNAL_BILLER.md) for the complete
setup, phone script, remote hosting option, and tests. This path uses the
original app and does not require the standalone rehearsal below.

## Earlier standalone bill-payment rehearsal

Run the complete simulated flow with no API credentials:

```sh
python -m scripts.autopay_demo
```

Open `http://127.0.0.1:5010` and enter the demo token printed in the terminal.
Click **Yes**, then **+1 day** twice: expected income settles, a $120 bill is
paid once in the **local synthetic ledger**, and a confirmation appears.
Auto-play, cancellation, family approvals, delayed income, and reset are included.

For real phone interaction, the same runner supports `--whatsapp` with locally
configured Twilio credentials and a signed public webhook. Money remains
simulated; it does not write to live Nessie or pay a utility. Rishy's original
read-only server is a separate entry point and remains unchanged.
See [the autopayment guide](docs/AUTOPAY_DEMO.md) for exact setup and boundaries.

## Synthetic bill data

Four fictional fixed-income profiles and fifteen repeatable scenarios are
available for account questions now and payment/monitoring acceptance tests
as those features are implemented. No API credentials are needed to inspect them:

```sh
python -m scripts.demo_data list
python -m scripts.demo_data validate
python -m scripts.demo_data facts --scenario safe_payment
python -m scripts.demo_data export --scenario wait_for_income --output work/robert.json
```

To use them with the current WhatsApp app, run the read-only local Nessie fixture:

```sh
python -m scripts.demo_data serve --scenario safe_payment
```

Then start the WhatsApp app with `NESSIE_BASE_URL=http://127.0.0.1:5051`,
`NESSIE_API_KEY=synthetic-not-a-secret`, and `STEADY_CUSTOMER_NAME=Margaret`
in its process environment. Twilio/OpenAI setup is still required for WhatsApp.
The fixture server cannot execute payments and never writes to real Nessie.
See [the synthetic data guide](docs/SYNTHETIC_DATA.md) for exact commands,
scenario outcomes, date rebasing, reset behavior, and the money-data contract.

## Watcher

The Watcher remembers her account between passes and decides when something is
worth a text. Code decides every pattern and every number. The model may only
reword a finished sentence, and its wording is dropped if it adds a dollar
amount, a date or a weekday that the fact sheet lacks.

One pass pulls Nessie and diffs it against the last snapshot. It then rebuilds
her rhythm from completed records, judges what changed, skips anything already
emitted, stores the result and hands it off. The first pass is a baseline, so
past purchases never alert.

| Pattern | Who sees it |
| --- | --- |
| Bill went up, duplicate charge, new monthly charge, purchase that would leave $200 or less after upcoming bills, usual deposit late, payment she put on hold | Mom, over WhatsApp, only inside the 24-hour session. Otherwise it waits in the queue |
| Bill due within 14 days | Planner queue (`consumer=planner`), silent |
| One charge somewhere new | Stored only, silent |

Mom can also ask "What if I spend $1,900 at the dentist?". Code reads the
amount from her text. Nothing is stored.

```sh
python -m backend.watch.run                    # one pass against live Nessie
python -m backend.watch.run --date 2026-10-24  # pin the clock
python -m backend.watch.demo                   # fixture shapes, no network
```

To show an alert on WhatsApp without touching Nessie, have Mom text the bot
once, then stage a scenario against the running server:

```sh
python -m backend.watch.stage              # list: duplicate, subscription, bill_increase,
                                           #       shortfall, deposit_missing, held
python -m backend.watch.stage duplicate    # memorize a baseline, press Enter, new charge -> text
```

Staged scenarios use synthetic accounts and their own memory, `data/demo.sqlite`.

Memory lives in `data/ledger.sqlite`, which is gitignored. Delete it to start
over from a first look. Decisions (accepted, declined, held) are append-only.

Routes for teammates on the same machine. They refuse any request relayed
through the tunnel (403):

| Route | Purpose |
| --- | --- |
| `POST /watch/run` | One pass. Optional JSON `{"date": "YYYY-MM-DD"}` |
| `GET /watch/events?consumer=planner&delivered=false` | Stored verdicts |
| `POST /watch/decisions` | `{"payee", "decision", "amount", "payment_date"}`. Appends to the tape |
| `POST /watch/check` | `{"merchant", "amount"}`. Judges a purchase she is considering |

## Development

Andrew works on `andrew`. Team changes should go through pull requests to `main`
once the team establishes that branch with shared history.

For each new clone, enable remembered merge conflict resolutions:

```sh
git config --local rerere.enabled true
git config --local rerere.autoupdate false
git config --local pull.ff only
```

When integrating an existing `main` into a clean working branch:

```sh
git fetch origin
git merge origin/main
```

Resolve conflicts using both contributors' intent and run the relevant checks.
Git remembers resolutions; agents still need to review and validate them.
