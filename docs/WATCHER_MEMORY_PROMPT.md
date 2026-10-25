# Implementation prompt: Watcher (memory + monitoring)

Follow this prompt. Read `docs/FEATURES_AND_GOALS.md` and `docs/PROJECT_CONTEXT.md` before writing code. This task builds Person 2's slice: feature 1's outreach trigger, and feature 4, memory-driven monitoring.

The LLM handles language. Code handles money. Code decides whether a pattern fired. The model only turns a fact sheet into a sentence.

## What this slice is

Watcher keeps four records for Margaret.

| Record | What it holds | When it changes |
| --- | --- | --- |
| Frame | Last Nessie snapshot, by `_id` | Every watch pass, after judging |
| Rhythm | What completed history repeats | Rebuilt from completed records every pass. Pending bills are not history |
| Pocket | The $200 buffer plus bills due on or before the next deposit | Computed each judgment. Never stored as a guess |
| Tape | Decisions Mom or Sarah actually made | Append-only, only through `record_decision` |

A watch pass emits verdicts. A verdict is a fact sheet plus a consumer. Person 1's planner receives `bill_due`. Mom receives a text only when `notify` is true. Everything else stays in memory.

This slice does not pick a payment date, approve or deny a payment, or write to Nessie. `backend/money/contracts.py` stays unimplemented. Planner, safety, and executor belong to other people. Watcher hands them facts.

## When to speak

These six patterns are the only Mom-facing alerts. The sentence under Example is the deterministic fallback. Tests assert that sentence. The model may rewrite it only after the fact sheet exists, and only if every dollar, date, and weekday it uses is already in that sheet.

| Pattern | Kind | When to flag | Example response |
| --- | --- | --- | --- |
| Recurring bill increases | `bill_increase` | A `steady` bill's new pending amount is higher than its usual band | "Your internet bill increased from $65 to $85." |
| Possible duplicate charges | `duplicate` | Same merchant and amount, as separate completed charges, 0 to 2 days apart, and at least one is new since the frame | "There are two $120 dentist charges today. Were you expecting both?" |
| New subscriptions | `subscription` | A second matching charge appears 25 to 35 days after the first, and that second charge is new since the frame | "StreamBox charged $14.99 again. This may be a monthly subscription." |
| Pocket shortfall | `pocket_shortfall` | A purchase plus bills due through the next deposit leaves $200 or less | "This would leave $147.75 after your upcoming bills." |
| Expected deposit missing | `deposit_missing` | A `steady` deposit has no completed or pending row after its arrival window | "Your usual deposit hasn't appeared yet. Let's check before scheduling payments." |
| A held payment reappears | `held_payment` | A payee on the tape as `declined` or `sarah_denied` shows up again | "You already put this payment on hold. It's still on hold." |

One verdict per subject. First match wins:

1. `held_payment`
2. `duplicate`
3. `pocket_shortfall`
4. `subscription`
5. `bill_increase`
6. `deposit_missing` is account-level. It emits even when a purchase verdict also emits. It is not dropped because a bill matched something else.
7. `bill_due` — pending bill inside the 14-day window, not on the tape as `accepted`. `notify: false`. Consumer: `planner`.
8. `new_merchant` — one completed charge, pocket holds, no other pattern. `notify: false`. Consumer: `memory_only`.

If a duplicate also breaks the pocket, the kind stays `duplicate` and the shortfall stays in the facts. The spoken sentence stays the duplicate question. If a subscription also breaks the pocket, the kind stays `pocket_shortfall` and `subscription: true` is in the facts. The spoken sentence stays the pocket sentence.

No verdict, and no text, when a purchase fits the pocket and matches none of the six. A grocery trip inside her H-E-B range is silence.

## Product rules

Still in force from the features doc:

- Threshold $500. Buffer $200. Known payees include Austin Energy.
- Never show Sarah the balance. This slice does not text Sarah. A verdict with `consumer: "safety"` omits balance, pocket, and shortfall.
- Never let the LLM invent, add, subtract, or round money or a date.
- Never guess an unclear text.
- Short sentences. No jargon. One point per text.
- Demo customer is Margaret. Discover her with the existing Nessie client. Do not hardcode a customer id or account id.

Rules that keep these patterns honest:

- Never alert because a purchase is above the average of its merchant or category. Categories are labels, never thresholds.
- Never say "usually" unless that rhythm status is `steady`.
- Never call one charge a subscription.
- Never call a bill decrease an increase. A drop is stored, not texted.
- The first watch pass is a baseline for purchases. Historical purchases do not notify, including old duplicates and old subscription pairs. Exceptions that do fire on first look: `bill_due`, a pending `bill_increase`, `deposit_missing`, and `held_payment`.
- Fuzzy name matching is forbidden. Normalize with code, then compare exactly.
- The Nessie sandbox does not lower the checking balance when purchases are added. Use the balance the account endpoint returns. Do not subtract purchases from it.
- The word "today" is allowed only when the fact sheet says `same_day_as_clock: true`. Otherwise name the date from the sheet.
- Do not weaken the weekday ban in `backend/agent/reply.py`. Watcher wording is a separate checker. A weekday is allowed only when code put it in the fact sheet.

## Pipeline

One function, `run_watch(clock) -> list[verdict]`. No background thread. The demo fires it on purpose. `clock` is an injected `datetime.date`. Tests pass a fixed day. The default is today. The window is 14 days, one constant.

1. **Pull.** Existing `NessieClient`: checking balance, bills, deposits, purchases. Read-only.
2. **Diff** against the frame by `_id`. Added, changed (amount, date, or status), or dropped. A dropped id is stored and not texted.
3. **Rebuild rhythm** from completed bills, completed purchases, and completed deposits. Pure function of the pull.
4. **Judge** added and changed items, the pocket, the tape, and the deposit calendar.
5. **Dedupe.** The same `dedupe_key` is never notified twice. A second pass with no changes emits nothing new.
6. **Store** the frame, the rhythm, and the verdicts.
7. **Hand off** every `bill_due`. **Word** every verdict with `notify: true`.

A contemplated purchase (she asked "what if") does not enter the frame, the rhythm, or the dedupe store. Asking twice answers twice.

## Rhythm

Pure function. Completed records only. Amounts are integer cents via `Decimal`, quantized to cents. Reject bools. No floats in the judge.

Keys: casefold, turn `&` into `and`, strip punctuation, collapse whitespace. `H-E-B` and `HEB` share a key. Display name is the most recent raw name. No edit distance. Deposit keys use the same function on the description (`Social Security`).

| Field | Rule |
| --- | --- |
| `completed_count` | Completed records for this key |
| `median_cents`, `low_cents`, `high_cents` | Present only when count ≥ 2. Otherwise null |
| `previous_cents` | Most recent completed amount. Null when count is 0 |
| `gaps_days` | Days between completed dates, in order |
| `day_of_month` | The day, when every completed record falls on that same day. Else null |
| `status` | Below |

Status:

- `sketch` — fewer than 2 completed records. No baseline. Do not say "usual."
- `steady` — 2 or more, and the spread (`high - low`) is within `max(100 cents, 2% of the median)`. Fixed bills, subscriptions, and Social Security live here.
- `uneven` — 2 or more, and the spread is wider. Groceries live here. Store the low and the high. Do not reduce them to a normal amount.

H-E-B at $47 and $62 is `uneven`. Shell, CVS, and Walgreens, one purchase each, are `sketch`. One completed Austin Energy bill is `sketch`, so $118.60 versus a pending $120.00 is not a bill increase.

## The six patterns

### Recurring bill increases

All of these:

- The payee's completed rhythm is `steady`.
- A pending bill for that payee is in this pull.
- The pending amount is **greater** than the most recent completed amount.
- The pending amount sits above the band by more than `max($5.00, 10% of the median)`. Compare in cents.

Speak: "Your {display name} bill increased from {previous} to {pending}."

`$65` to `$85` fires. `$65` to `$67` does not (inside the band). A decrease does not fire. One completed bill does not fire. Dedupe key: `bill_increase:{bill_id}:{pending_cents}`. Fires on first look when the high pending bill is already there, once.

### Possible duplicate charges

All of these:

- Two completed purchases, different `_id`s, same merchant key.
- Amounts differ by at most 100 cents.
- Gap is 0, 1, or 2 days.
- This is not the first look, and at least one of the two ids is new since the frame.
- The pair is not a subscription gap. Those windows do not overlap.

Speak, when both dates equal `clock`: "There are two {amount} {merchant} charges today. Were you expecting both?"

When the dates are not the clock date, name the fact-sheet date instead of "today." Do not reverse, dispute, or delete the charge. Dedupe key: `duplicate:{merchant_key}:{amount_cents}:{earlier_date}`.

### New subscriptions

All of these:

- The charge is new since the frame.
- The merchant has exactly one earlier completed purchase.
- The two amounts differ by at most 100 cents.
- The gap is 25 to 35 days, inclusive.

Speak: "{merchant} charged {amount} again. This may be a monthly subscription."

A third month does not lock again. Both charges already in the baseline do not notify; the merchant is `steady`. One charge alone is `new_merchant`, silent. Dedupe key: `subscription:{merchant_key}:{amount_cents}`.

### Pocket shortfall

Reserved bills = pending bills whose payment date is on or before the horizon.

- Horizon = earliest pending deposit date.
- A bill due the same day as the deposit is reserved. Do not assume the deposit posts first.
- A bill due after the horizon is the next cycle. With the seed, Spectrum on Nov 12 is after Social Security on Nov 3, so it is not reserved.
- If a `deposit_missing` verdict is open, do not use the missed deposit as the horizon. Horizon = `clock + 14 days`, and `horizon_reason` is `deposit_missing`.
- If no pending deposit is on file and none is missing, horizon = `clock + 14 days`, and `horizon_reason` is `no_deposit_on_file`. The sentence must not say "payday."

The pocket fails when:

`balance_cents - purchase_cents - reserved_cents <= 20000`

Equal to $200 fails. The features doc says the balance after a payment stays **above** $200. Use the same inequality.

`left_after_cents = balance_cents - purchase_cents - reserved_cents`

Speak, contemplated: "This would leave {left_after} after your upcoming bills."

Speak, already posted: "This leaves {left_after} after your upcoming bills."

The fallback does not name a weekday. The fact sheet still carries each reserved bill's payee, amount, date, and weekday, so a grounded rewrite may add them and an ungrounded one may not. Dedupe posted shortfalls by purchase id. Do not dedupe a contemplated check.

Worked live shape, clock 2026-10-24: balance `$2,213.00`, reserved Austin Energy `$120.00` (Oct 31) + Water Co `$45.25` (Nov 3) = `$165.25`. Spectrum is not reserved.

| Purchase | Result |
| --- | --- |
| `$1,900.00` | Shortfall. Left after `$147.75`. Sentence: "This would leave $147.75 after your upcoming bills." `over_threshold: true` |
| `$1,847.75` | Shortfall. Left after is `$200.00`, which is not above the buffer |
| `$1,847.74` | Pocket holds. No text |
| `$40` at H-E-B | Pocket holds. No text. Do not call H-E-B unusual |

`$1,847.75` is `221300 - 16525 - 20000` cents.

### Expected deposit missing

All of these:

- The deposit description's rhythm is `steady`.
- `day_of_month` is set. Next expected date is that day in the month after the last completed deposit. If that day does not exist (the 31st), use the last day of the month. Do not add the median gap; month lengths would drift the 3rd onto the 4th.
- Arrival window = expected date through expected date + 2 days, inclusive.
- `clock` is after that window.
- No completed deposit of that key has a date on or after the expected date.
- No pending deposit of that key is dated inside the window.

Speak: "Your usual deposit hasn't appeared yet. Let's check before scheduling payments."

Do not put a dollar amount in that fallback. The usual amount and the expected date are still in the facts. Fires on first look. Dedupe key: `deposit_missing:{deposit_key}:{expected_date}`. When a later pull contains the deposit, clear the flag and send nothing.

While the flag is open, every `bill_due` in that pass includes `hold_scheduling: true`. Person 1 must not propose a date while it is set. Watcher still emits `bill_due` so the planner can see the bills. Watcher does not propose a date itself.

Seed data must not fire this. Completed Social Security on Aug 3, Sep 3, and Oct 3, plus a pending deposit on Nov 3, clock Oct 24: the next expected date is Nov 3, and a pending row covers it.

Fixture that must fire: completed Social Security on Jul 3, Aug 3, and Sep 3, nothing in October, clock Oct 6. Expected date Oct 3. Window ends Oct 5.

### A held payment reappears

Tape rows are append-only: `payee_key`, optional `amount_cents`, optional `payment_date`, `decision` (`accepted` | `declined` | `undone` | `sarah_denied`), `recorded_at`.

The only writer is `record_decision`. Nothing infers a decision from tone, from a posted purchase, or from the model. This slice does not turn a WhatsApp "yes" into a tape row. Paying stays off.

Match on `payee_key` only. A new amount still matches.

- `declined` or `sarah_denied` → `held_payment`, notify Mom. Speak: "You already put this payment on hold. It's still on hold." Do not say Sarah said no. Do not also emit `bill_due` for that bill.
- `accepted` for that payee and payment date → suppress `bill_due`. Send nothing.

Dedupe key: `held_payment:{payee_key}:{bill_id}`.

Set `over_threshold: true` when a subject amount is over $500. That flag is for safety. It does not text Sarah and it does not put the balance on a safety payload.

## Fact sheet

Code fills this before the model sees it. Amounts are formatted (`$1,900.00`). Dates are formatted (`Oct 31`). Weekdays come from `date.strftime`. Nulls stay null. `source` is `nessie` or `fixture`. Fixture logs and payloads keep `source: "fixture"`.

```json
{
  "kind": "pocket_shortfall",
  "source": "nessie",
  "notify": true,
  "consumer": "mom",
  "dedupe_key": "pocket_shortfall:contemplate:190000",
  "over_threshold": true,
  "facts": {
    "merchant": "Dentist",
    "purchase_amount": "$1,900.00",
    "when": "contemplated",
    "balance": "$2,213.00",
    "buffer": "$200.00",
    "left_after": "$147.75",
    "horizon_date": "Nov 3",
    "horizon_reason": "next_deposit",
    "horizon_label": "Social Security",
    "same_day_as_clock": false,
    "reserved_bills": [
      {"payee": "Austin Energy", "amount": "$120.00", "date": "Oct 31", "weekday": "Saturday"},
      {"payee": "Water Co", "amount": "$45.25", "date": "Nov 3", "weekday": "Tuesday"}
    ],
    "subscription": false
  }
}
```

## Wording

Send the fallback on model error, or when the rewrite contains a dollar, a `Mon D` date, or a weekday that is not in the fact sheet. "Tomorrow" and "yesterday" always fail. "Today" fails unless `same_day_as_clock` is true.

Question answering stays on its current path. Ordinary bill questions do not go through Watcher.

## Contemplated purchase

Add the intent `contemplate`: she is asking whether a purchase fits, not asking to pay.

- "What if I spend $1,900 at the dentist?" → `contemplate`
- "Can I afford $40 at H-E-B?" → `contemplate`
- "Pay the dentist $1,900" → `pay_request`, unchanged. Reply remains "Paying is not turned on yet."
- Unsure → `unclear`. No Nessie call.

Code parses the dollar amount from her text. The model does not supply the number. No amount → re-ask "How much would it be?" Keep a merchant only when that string, casefolded, is a substring of her text. Otherwise re-ask who it would be paid to. The loop runs the pocket judge on live reads and returns the grounded sentence.

## Storage and entries

SQLite at `data/ledger.sqlite`, gitignored. Tables: `frame`, `rhythm`, `tape`, `verdicts`. Deleting the file makes the next pass a first look.

No vector store. No embeddings. No chat transcript as memory.

| Entry | Behavior |
| --- | --- |
| `POST /watch/run` | One live pass. Returns verdicts |
| `GET /watch/events` | Stored verdicts. Filter by `consumer` and `delivered` |
| `POST /watch/decisions` | Append a tape row |
| `POST /watch/check` | Contemplated purchase against live Nessie. Does not store a purchase |
| `python -m backend.watch.run` | Same as `/watch/run` |
| `python -m backend.watch.demo` | Prints the fixtures with `source: fixture` |

Default planner sink stores `bill_due` with `delivered: false`. It does not import a planner or call `propose_date`. `GET /watch/events?consumer=planner&delivered=false` is Person 1's queue.

Existing WhatsApp behavior stays: signature check, empty TwiML, read-only questions, pay-not-on. `contemplate` is the only new inbound branch.

## Fixtures tests must lock

Clock and cents are fixed. Do not ask the model for these results.

**Live shape, first look, clock 2026-10-24.** Balance `$2,213.00`. Pending Social Security `$1,874.00` on Nov 3. Pending bills: Austin Energy `$120.00` Oct 31, Water Co `$45.25` Nov 3, Spectrum `$89.99` Nov 12. One completed Austin Energy `$118.60`. Purchases: H-E-B `$47` and `$62`, Shell `$34` once, CVS `$18` once, Walgreens `$15` once. Deposits completed Aug 3, Sep 3, Oct 3.

Expect three `bill_due` verdicts, H-E-B `uneven`, the other merchants `sketch`, no purchase alert, no bill increase, no missing deposit. A second identical pass emits nothing new.

**Thursday shape, fixture only.** Clock Monday 2026-10-26. Balance `$400.00`. Austin Energy `$120.00` due Thursday 2026-10-29. Next deposit Nov 3. Contemplated `$100.00`. Left after `$180.00`. Fallback: "This would leave $180.00 after your upcoming bills." Fact sheet weekday is Thursday. `source` is `fixture`.

**Bill increase.** Two completed internet bills at `$65`, one pending at `$85`, display name `internet`. Fallback: "Your internet bill increased from $65 to $85." A pending `$67` does not fire. A single completed `$65` does not fire.

**Duplicate.** Frame holds one completed dentist purchase, `$120`, dated the clock day. Pull adds a second completed `$120` the same day, different id. Fallback: "There are two $120 dentist charges today. Were you expecting both?" Both charges already on the frame at first look do not fire. A second `$120` thirty days later is a subscription, not a duplicate.

**Subscription.** Frame holds StreamBox `$14.99` on Sep 24. Pull adds StreamBox `$14.99` on Oct 24. Fallback: "StreamBox charged $14.99 again. This may be a monthly subscription." One charge alone is silent. Both already on the first-look frame are silent and `steady`. A `$40` second charge does not lock. A 10-day gap does not lock.

**Missing deposit.** The October fixture above. Fallback is the deposit sentence with no dollar amount. The seed shape, which includes Nov 3 pending, does not fire. A deposit rhythm with one completed row does not fire.

**Held payment.** Tape: Roofer `sarah_denied`. A new Roofer bill is `held_payment` and not `bill_due`. Fallback: "You already put this payment on hold. It's still on hold." An `accepted` row for Austin Energy on Oct 31 suppresses that `bill_due`.

**Grounding.** A rewrite that adds a dollar, a date, or a weekday absent from the sheet is dropped. The fallback is sent.

## Layout

```
backend/watch/rhythm.py     # pure: completed records -> rhythm
backend/watch/pocket.py     # pure: balance, bills, deposits, purchase -> pocket
backend/watch/judge.py      # pure: diff + rhythm + pocket + tape -> verdicts
backend/watch/ledger.py     # sqlite
backend/watch/run.py        # pull, diff, rebuild, judge, store, hand off
backend/watch/wording.py    # fallback, then grounded rewrite
backend/watch/demo.py       # fixtures, labeled fixture
tests/test_watch_rhythm.py
tests/test_watch_pocket.py
tests/test_watch_judge.py
```

`rhythm.py`, `pocket.py`, and `judge.py` import neither Nessie, nor the LLM, nor Flask.

Wire `contemplate` through `backend/agent/intent.py` and `backend/agent/loop.py`. Keep the confidence floor. An unparseable amount never reaches the judge.

## Done when

- The six example sentences are the fallbacks for their fixtures.
- First look on the live shape emits three `bill_due` verdicts and no purchase alerts.
- `$1,900` against that shape leaves `$147.75`. `$40` at H-E-B does not alert.
- StreamBox locks only on the new second charge about a month later.
- A missing October Social Security fires. The seeded November pending deposit does not.
- A held payee cannot return as a fresh `bill_due`.
- Mom can text "What if I spend $1,900 at the dentist?" and the reply uses live Nessie bills. "Pay Austin Energy" still does not pay.
- `GET /watch/events?consumer=planner` shows the handoff. Nothing here chooses a pay date. `hold_scheduling: true` is set on those rows while a deposit is missing.
- `python -m pytest` passes, including the existing WhatsApp tests.
- `data/ledger.sqlite` is gitignored. `.env` stays untracked.

## Do not

- Do not build the date picker, the payment gate, execute, cancel, or the scam reply.
- Do not text Sarah, and do not put the balance on a safety payload.
- Do not add a spending forecast, a monthly budget, or a category alert.
- Do not store conversation transcripts as the memory.
- Do not let the model decide that a pattern fired, pick a merchant key by similarity, or compute the shortfall.
- Do not seed or delete live Nessie data from the watch path. Fixtures stay in tests and in `backend.watch.demo`.
- Do not notify on the first look for purchases already in history.
- Do not treat two grocery trips of different amounts as duplicates.
