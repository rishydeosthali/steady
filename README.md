# Steady

A proactive financial agent for older adults, through Slack direct messages.
Steady watches for upcoming bills, plans payment timing around income, and
requests senior and caregiver approval before simulated settlement. The Slack
demo uses a persistent fictional account; no real money moves.

**The LLM handles judgment and language; code handles money.**

See [the project brief](docs/PROJECT_CONTEXT.md) for features, architecture, and
the HackTX demo, and [AGENTS.md](AGENTS.md) for collaboration and conflict handling.

## Hosted scenario studio

The Slack app has an opt-in persistent simulation dashboard. Add
expenses, change bills, delay/settle income, advance the demo date, and save or
reset scenarios through a website. Agent replies and payment checks share that
same fictional account. No live banking writes occur; Slack messages are real.

Deploy the included `render.yaml` through Render's website for an always-on
service with persistent storage and no local Cloudflare tunnel. Set
`STEADY_CHANNEL=slack` and the Slack settings below in Render; adjust the
blueprint messaging settings accordingly. Twilio credentials are unused. See the
[scenario studio guide](docs/SCENARIO_STUDIO.md) for setup and account steps.

## Slack agent

Margaret sends Steady a Slack DM. Slack posts a signed event to the app, which
acknowledges it immediately, processes it in a worker, and replies through the
Slack API. Account questions, monitoring, and payment checks share the scenario
studio's fictional account. Slack currently requires `STEADY_SIMULATION=1`;
Twilio and Nessie credentials are unused. Bill-photo attachments are not yet
supported in Slack mode.

```text
Slack DM -> /slack/events (signature checked, acknowledged immediately)
                 -> worker -> agent/loop.py -> shared simulated account
                 -> grounded reply -> slack_client.py -> Slack DM
```

### Setup

```sh
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # Windows: copy .env.example .env
```

Set these values in your private `.env` or hosting environment:

```dotenv
STEADY_CHANNEL=slack
STEADY_SIMULATION=1
STEADY_EXTERNAL_BILLER=0
STEADY_AUTONOMY=1
STEADY_SIMULATION_DIR=work/slack-simulation
OPENAI_API_KEY=<your OpenAI API key>
SLACK_BOT_TOKEN=<your bot token>
SLACK_SIGNING_SECRET=<your signing secret>
SLACK_TEAM_ID=<your workspace ID>
SLACK_SENIOR_USER_ID=<the senior's member ID>
SLACK_CAREGIVER_USER_ID=<a different member's ID>
```

Never commit credentials. Install the bot with `chat:write`, `im:history`, and
`im:write`; enable App Home messages. Subscribe to `message.im` at the public
HTTPS `/slack/events` endpoint. Enable Interactivity at `/slack/actions` for
caregiver approval buttons. Only the configured caregiver can approve or deny;
the senior cannot approve their own payment. If the caregiver Slack ID is
omitted, configure the email provider described in the external-biller guide.

Run locally with `python -m backend.app` on port 5000. For Slack callbacks,
expose it with `cloudflared tunnel --url http://127.0.0.1:5000` and use the
HTTPS origin in both Slack request URLs. Send a DM from the configured senior
account to start. Slack has no 24-hour messaging window or template requirement.

Requests require a fresh valid Slack signature and the configured workspace and
user. Bot messages, edits, channel messages, and repeated event IDs are ignored.
Event IDs persist, but a process crash after acknowledgment may require a new
message. Use a fresh simulation directory when switching channels.

### Example conversations

| Message | Result |
| --- | --- |
| "What is my balance?" | Current simulated checking balance |
| "What bills do I have?" | Known bills and statuses |
| Deposits or spending question | Facts from the fictional account |
| "What if I spend $1,900 at the dentist?" | Deterministic affordability check; no purchase is made |
| YES or NO after a bill proposal | Exact consent handling; include the reference when multiple decisions are open |
| Unclear text | Clarification rather than a guessed action |

Amounts and dates in generated account replies are checked against retrieved
facts. Unsupported wording falls back to a plain rendering of those facts.
Unknown information stays unknown. See [the Slack guide](docs/SLACK.md).

### Tests

```sh
python -m pytest
```

Tests use dummy credentials and fakes; integration tests use local HTTP.
No live service credentials or `.env` are required.

## Separate integration: external demo utility

The original Twilio backend also supports an independent HTTP biller API with
its own database and receipt page. This uses Nessie sandbox reads for funds
checks and simulated settlement; Nessie is not debited. It is separate from
Slack mode, which requires the shared local simulation.

See [the external-biller guide](docs/EXTERNAL_BILLER.md) for its setup, approval
flow, independent receipts, and tests.

## Earlier standalone bill-payment rehearsal

Run the complete simulated flow with no API credentials:

```sh
python -m scripts.autopay_demo
```

Open `http://127.0.0.1:5010` and enter the demo token printed in the terminal.
Click **Yes**, then **+1 day** twice: expected income settles, a $120 bill is
paid once in the **local synthetic ledger**, and a confirmation appears.
Auto-play, cancellation, family approvals, delayed income, and reset are included.

This runner is separate from the Slack app and uses its own local synthetic
ledger. See [the autopayment guide](docs/AUTOPAY_DEMO.md) for setup and boundaries.

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

These fixture tools are separate from the Slack dashboard's persistent account.
See [the synthetic data guide](docs/SYNTHETIC_DATA.md) for scenario outcomes,
reset behavior, and the money-data contract.

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
| Bill went up, duplicate charge, new monthly charge, purchase that would leave $200 or less after upcoming bills, usual deposit late, payment she put on hold | Senior's Slack DM in simulation mode |
| Bill due within 14 days | Planner queue (`consumer=planner`), silent |
| One charge somewhere new | Stored only, silent |

Mom can also ask "What if I spend $1,900 at the dentist?". Code reads the
amount from her text. Nothing is stored.

```sh
python -m backend.watch.run                    # one pass against live Nessie
python -m backend.watch.run --date 2026-10-24  # pin the clock
python -m backend.watch.demo                   # fixture shapes, no network
```

To show an alert in Slack, use the scenario dashboard to add an expense,
change a bill, or delay income. The simulation scheduler runs monitoring and
payment checks against that same account. Watcher memory lives in the
simulation directory's `watch.sqlite3`; decisions are append-only.

Routes for teammates on the same machine. They refuse any request relayed
through the tunnel (403):

| Route | Purpose |
| --- | --- |
| `POST /watch/run` | One pass. Optional JSON `{"date": "YYYY-MM-DD"}` |
| `GET /watch/events?consumer=planner&delivered=false` | Stored verdicts |
| `POST /watch/decisions` | `{"payee", "decision", "amount", "payment_date"}`. Appends to the tape |
| `POST /watch/check` | `{"merchant", "amount"}`. Judges a purchase she is considering |

## Persistent autonomous goals

Set `STEADY_AUTONOMY=1` to enable the goal coordinator in `backend.app`.
It maintains a confirmed savings/budget goal in private SQLite memory, chooses
tools with the model, observes their results, and resumes after changes or a
scheduled check. With `STEADY_EXTERNAL_BILLER=1`, it also tracks each external
invoice through verified simulated settlement and can replan funds-held bills
with fresh senior and caregiver approvals.

Text: `Save $300 by December 25, 2026, keep a $200 cushion, and allow $100 weekly
for essentials.` Steady reads it back and asks for `CONFIRM GOAL <reference>`.
`How is my budget?`, `PAUSE GOAL <reference>`, and `RESUME GOAL <reference>` work
in the same signed Slack DM flow. No savings transfer is performed.

See [autonomous goals](docs/AUTONOMOUS_GOALS.md) for architecture, setup,
limits, tests, and local inspection routes.

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
