# Synthetic data for Steady

This dataset is entirely fictional and designed for repeatable engineering
tests, not to represent older adults statistically. Names end in `Demo`, ids
start with `synthetic-`, and there are no phone numbers, credentials, real
customer records, or personal addresses. Retirement income amounts and dates
are invented; they are not benefit estimates or government payment calendars.

## What is included

- Four profiles: Margaret (stable fixed income), Robert (tight timing before
  income), Linda (insufficient funds), and James (comfortable buffer, useful
  for testing approval rules independently of affordability).
- Two previous paid observations per household bill, three historical income
  deposits, twelve weeks of grocery/transport/pharmacy spending, and three
  monthly subscription observations where applicable.
- One future expected income deposit per profile, plus upcoming bills.
- Fifteen scenario definitions with human-authored expected outcomes, sample
  incoming texts, and owning roles.
- A data loader, JSON exporter, and a read-only local Nessie fixture server
  that work with Rishy's existing `NessieClient`, agent, and WhatsApp webhook.

The baseline Margaret snapshot preserves the existing seeder's $2,213 opening
balance and upcoming bills: Austin Energy $120, Water Co $45.25, and Spectrum
$89.99. Its generated history is separate from the records in
`scripts/seed_margaret.py`; that existing live-sandbox script is unchanged.

## Quick start: no credentials or network

From the repository root, activate the environment installed using README:

```sh
source .venv/bin/activate
python -m scripts.demo_data list
python -m scripts.demo_data validate
python -m scripts.demo_data facts --scenario safe_payment
python -m scripts.demo_data export --scenario wait_for_income --output work/robert.json
```

`facts` exercises the existing Nessie read client and amount/date formatter
in memory. `export` writes the complete snapshot, including expectations for
test authors. Both commands are deterministic and make no API calls.

Dates are anchored to October 24, 2026 by default, never to the machine clock.
To rehearse on another date, pass `--as-of YYYY-MM-DD` to `facts`, `export`, or
`serve`. The loader rebuilds upcoming dates, historical months, and expected
dates together, handling short months and leap years. Use the returned
`as_of` as the test clock in future scheduler/payment tests.

## Use with the current WhatsApp app

Terminal 1, with the virtual environment active:

```sh
python -m scripts.demo_data serve --scenario safe_payment --as-of 2026-10-24
```

Terminal 2, with your existing Twilio/OpenAI settings from `.env`:

```sh
source .venv/bin/activate
NESSIE_BASE_URL=http://127.0.0.1:5051 \
NESSIE_API_KEY=synthetic-not-a-secret \
STEADY_CUSTOMER_NAME=Margaret \
python -m backend.app
```

Keep the normal tunnel and Twilio webhook pointing to the WhatsApp app on
port 5000. Only the app's Nessie reads now go to the local fixture on 5051.
This path still uses real Twilio and OpenAI for messages and language calls.
For another scenario, restart Terminal 1 with its id and match
`STEADY_CUSTOMER_NAME` to its profile's first name. The process environment
overrides `.env` without editing it; omit those overrides to use real Nessie.

The fixture server binds to loopback only. It has no payment/write endpoints,
does not connect to the real Nessie sandbox, and never returns scenario
expectations or approval metadata through the bank endpoints. `/healthz`
identifies the scenario as synthetic. A write returns HTTP 405, rather than
pretending a bill was paid.

## Use directly in Python tests or a future demo controller

```python
from datetime import date
from backend.demo.data import load_scenario
from backend.demo.nessie import fixture_client
from backend.agent.facts import gather_facts

snapshot = load_scenario("wait_for_income", as_of=date(2026, 10, 24))
nessie = fixture_client(snapshot)
account = nessie.resolve(snapshot["customer"]["first_name"])
facts = gather_facts(nessie, account["_id"], ("balance", "bills", "deposits"))
# Inject `nessie` into Agent(llm=your_llm, nessie=nessie,
#                          customer_first_name="Robert").
```

Each load and fixture client owns fresh data. Reloading a scenario restores
its starting records, and modifying returned responses cannot alter later
reads. This supplies the data-reset boundary for Person 2; it does not reset
future payment databases, approval sessions, message ids, or real Nessie.

## Scenario handoff

Role numbers refer to Andrew's four work areas: 1 money brain, 2 watcher,
3 conversation, 4 family loop/demo. Gate and alert names below are test
expectation labels; the future money-layer owner should map them to the
application's eventual contract.

| Scenario id | Intended result | Roles |
| --- | --- | --- |
| `safe_payment` | Earliest safe date is day 0; familiar $120 bill still requires explicit user approval | 1, 2, 3 |
| `wait_for_income` | $600 electric bill waits until day 6 income; reserve $60 phone bill on day 5; family approval also needed | 1–4 |
| `income_delayed` | Deposit slips to day 13, after day 9 deadline; original plan is no longer safe | 1, 2, 4 |
| `insufficient_funds` | No safe date for the $85 phone bill before day 5 | 1, 2, 4 |
| `spending_shortfall` | $400 minus optional $150 purchase and $85 bill leaves $165, below $200 buffer | 1, 2 |
| `new_subscription` | One new $29 membership charge merits a question, not a definitive recurrence/fraud claim | 2 |
| `subscription_increase` | $35 charge after three $15 monthly charges merits a price-change alert | 2 |
| `family_denies` | New $2,400 roofer stays unpaid after family denial | 1, 3, 4 |
| `family_approves` | Same roofer becomes eligible only after exact read-back and both approvals | 1, 3, 4 |
| `family_timeout` | Silence leaves the request held; it is never approval | 1, 4 |
| `gift_card_scam` | Gift-card demand is blocked; cited previous bill is completed | 1–4 |
| `duplicate_confirmation` | Two deliveries of the same confirmation produce at most one payment | 1, 3 |
| `cancel_scheduled` | Cancellation on day 1 prevents settlement on day 7 | 1, 3 |
| `exact_buffer` | $320 minus $120 leaves exactly the $200 buffer | 1 |
| `unknown_bill_amount` | Null amount stays unknown and cannot authorize payment | 1, 3 |

## Data contract and money semantics

Authored sources are `data/synthetic/profiles.json` and `scenarios.json`.
Use integer **cents** for all authored balances, amounts, and thresholds.
The loader converts to numeric USD dollars only for the Nessie read shapes
already consumed by this repository. Do arithmetic with integer cents or
`Decimal`, never the serialized floating-point dollar values.

| Snapshot key | Meaning |
| --- | --- |
| `as_of` | Frozen demo date; day 0 |
| `customer`, `account` | Fictional customer and current checking snapshot |
| `bills` | Nessie-shaped reads: `payee`, `payment_amount`, `payment_date`, `status` |
| `deposits` | Historical completed income plus a future pending expected deposit |
| `purchases`, `merchants` | Settled spending and merchant ids joined by `merchant_id` |
| `context` | App-only due dates, known payees, buffer, family threshold, and case inputs |
| `messages` | Scripted incoming texts; setup notes explain prerequisite read-backs/schedules |
| `expected` | Desired future feature outcomes; keep these out of model prompts |

Important conventions for test authors:

- The opening balance already includes **all completed historical** deposits,
  purchases, and bills. Never add/subtract that history again.
- A future pending deposit is expected income, not currently settled cash.
  Forecast tests assume it clears before debits on its date. A real executor
  must verify cleared funds and approvals again immediately before a write.
- Upcoming `pending` bill records are obligations in these read snapshots,
  not user authorization. For this dataset their `payment_date` is the nominal
  deadline, mirrored as `context.bill_metadata[id].due_date`. Approval starts
  false. A future planner should propose a separate execution date.
- The date expectations choose the **earliest** safe date through the target
  deadline, reserving other pending bills due by that deadline. This is an
  intentionally small benchmark, not a complete spending forecast.
- There are no additional forecasted future purchases except an explicit
  `proposed_purchase` what-if; historical spending does not silently become
  future cash-flow commitments.
- The benchmark treats `balance >= buffer` as safe. Confirm that equality
  rule with Person 1 before adopting it. The team's exact `$500` family-gate
  boundary is unresolved; these cases use amounts clearly below or above it.
- A family gate and affordability are separate checks. Approval cannot make
  an unaffordable payment safe. Family messages must exclude the balance.
- Every optional purchase is separate from settled purchase history. Extra
  completed purchases in a scenario are already included in its opening balance.
- Local ids are stable within a profile and deliberately **not** real Nessie
  object ids. Scenarios are alternatives; do not combine their same-id records.

## What this proves today

```sh
python -m pytest
```

Tests check source validation, exact-cent math, references, chronology,
deterministic reset/rebasing, independent cash-flow expectations, local HTTP
read-only behavior, and a signed WhatsApp question passing through the existing
agent with scripted language output. They run without network or credentials.

They do **not** prove live model intent quality, real Nessie writes, payment
idempotency, family approvals, monitoring, or cancellation. Those features
remain unimplemented in Rishy's current branch. Their scenario expectations
are acceptance examples for the relevant owners, not passing feature tests.

These exports are **read fixtures, not Nessie POST bodies**. They include local
ids, history, hypothetical obligations, and one intentionally invalid null
amount. Do not post the exported snapshot to real Nessie. Use the existing
`scripts/seed_margaret.py` when deliberately setting up its live demo customer;
that script writes to the sandbox and resets the account balance. Generalized
remote seeding/reset and live API schema verification are outside this change.
