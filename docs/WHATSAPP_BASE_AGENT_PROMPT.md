# Implementation prompt: WhatsApp base agent

Follow this prompt. Read `docs/FEATURES_AND_GOALS.md` before writing code and
treat it as the product spec. This task adds the WhatsApp channel and the
first working slice of the agent. It does not build the full three-beat demo.

## What to build

Connect Steady's backend to the Twilio WhatsApp sender already on the account,
and stand up the one agent loop from the features doc. For this slice, the
loop must answer ordinary questions about data already in Nessie. Do not move
money.

Mom texts the WhatsApp sender. Twilio POSTs to a webhook. The agent reads
Nessie, then replies on WhatsApp in the tone from the features doc: short
sentences, no jargon, one point per text.

## Product rules to keep

From `docs/FEATURES_AND_GOALS.md`:

- One agent loop. The LLM handles language. Code handles money and facts.
- Parse Mom's text into `yes`, `no`, `undo`, `question`, `forwarded message`,
  or unclear. Unclear input gets a re-ask. Never guess an intent.
- Never let the LLM invent a balance, bill, date, or payee. Replies about
  money may only use fields just returned by Nessie.
- Never create, cancel, or approve a payment in this slice. If Mom asks to
  pay, say this chat can look up her bills and balance, and that paying is
  not turned on yet.
- Demo person is Margaret. Known payee called out in the spec is Austin
  Energy. Threshold is $500. Buffer is $200. Those rules are not enforced as
  payments here. They are context for later slices.
- The features doc says the demo is simulator-only and has no Twilio channel.
  That is superseded for this task only: WhatsApp is Mom's text channel.
  Do not build the two-phone simulator, Sarah's co-sign flow, the scam
  reply, the scheduler, or the date picker in this slice. Leave clear
  function boundaries so those calls can be added later:
  get upcoming bills, propose date, request payment, execute, cancel,
  verify claim.

## Credentials

Secrets live in `.env` at the repo root. That file is gitignored. Read it.
Never print the auth token, the Nessie key, or the OpenAI key, never log
them, and never commit `.env`.

Use these variable names:

| Variable | Meaning |
| --- | --- |
| `TWILIO_ACCOUNT_SID` | Twilio account |
| `TWILIO_AUTH_TOKEN` | Twilio auth token |
| `TWILIO_WHATSAPP_FROM` | Steady's WhatsApp sender, `whatsapp:+17372583478` |
| `MOM_WHATSAPP_TO` | Mom's WhatsApp address, `whatsapp:+17372353170` |
| `TWILIO_CONTENT_SID` | Approved content template, `HXfe5ab5f00277942d4d4200328b4d403c` |
| `NESSIE_API_KEY` | Nessie sandbox key |
| `NESSIE_BASE_URL` | `https://api.nessieisreal.com` |
| `OPENAI_API_KEY` | OpenAI key already stored in `.env`. This is the only language model. |

If `.env` is missing those values, stop and say so. Do not hardcode them.

Copy `.env.example` when adding a new machine. `.env.example` has empty
secrets and the non-secret sender, recipient, content SID, and base URL.

## Nessie reads

Use the key as the `key` query parameter. Discover ids at runtime. Do not
hardcode a customer id or account id.

1. `GET /customers` and select the customer named Margaret. If none exists,
   stop and report the customer names that do exist. Do not create customers
   or bills in this slice.
2. `GET /customers/{id}/accounts` and use her checking account.
3. For a question, load only what the question needs:
   - account balance from the checking account
   - `GET /accounts/{id}/bills` for payee, amount, payment date, and status
   - `GET /accounts/{id}/deposits` for the Social Security deposit and any
     other deposits
   - purchases only when she asks about spending

Expose those reads as plain functions, not as LLM tool-calling that can
write. The model may see the function results. It may not choose a URL.

## Agent loop

On each inbound text:

1. Treat `Body` as untrusted user input. Put it in a user message. Do not
   concatenate it into the system prompt.
2. Ask the LLM for one intent label and, for a question, a short restatement
   of what fact is needed. If the label is not confident, the intent is
   unclear.
3. Branch:
   - **unclear:** send a re-ask. Do not call Nessie.
   - **question:** call the Nessie read functions, then ask the LLM to answer
     in Steady's tone using only that JSON. If the fact is missing, say
     Steady cannot see it.
   - **yes / no / undo / forwarded message:** recognize them and reply that
     this number can answer questions about Margaret's account, and that
     paying, undoing, and checking a forwarded text are not available yet.
     Do not call execute or cancel.

Keep the intent parser, the Nessie client, and the reply writer in separate
modules so a later slice can replace the non-question branches with the
contracts in the features doc.

## Twilio behavior

### Inbound webhook

- `POST /whatsapp/inbound`, form-encoded, as Twilio sends it.
- Validate `X-Twilio-Signature` with the Twilio SDK validator and the auth
  token before reading `Body`. Reject invalid signatures with 403.
- Read `From`, `To`, `Body`, and `MessageSid`. Ignore messages that are not
  from Mom's WhatsApp address unless you are deliberately testing from
  another joined sandbox participant. Log the sender without logging secrets.
- Twilio stops waiting at about 15 seconds. Nessie plus an LLM can miss that.
  Validate the signature, return an empty TwiML `<Response/>` immediately,
  then send the answer with the Messages API. Do not block the webhook on
  the model.

### Outbound

- Replies inside an open WhatsApp session use free-form `body`, `from_` =
  `TWILIO_WHATSAPP_FROM`, `to` = the inbound `From`.
- Use `TWILIO_CONTENT_SID` only to start a conversation when there is no
  recent inbound message. Match the template's variables. Do not invent
  variables. If the template variables are unknown, fetch the content
  template and fill only placeholders it defines.
- A small local command or `POST /demo/ping` may send that template to
  `MOM_WHATSAPP_TO` so a human can open the thread. The live path is still
  the webhook.

### Register the webhook

The sender is `whatsapp:+17372583478`, not the public Twilio sandbox number
`+14155238886`.

1. Start the app.
2. Expose it with a public HTTPS tunnel (ngrok or Cloudflare Tunnel). The
   webhook URL is `https://<tunnel-host>/whatsapp/inbound`.
3. Point "When a message comes in" at that URL, method POST.
   - If `+17372583478` is an incoming phone number on the account, set
     `sms_url` through the Twilio API.
   - If it is the WhatsApp sandbox sender, set it under Console → Messaging
     → Try it out → Send a WhatsApp message → Sandbox settings. Also set
     the status callback to `POST /whatsapp/status` if you add one.
4. Print the exact URL you used. If the API cannot update that sender, print
   the console path and the URL to paste. Do not finish without the webhook
   pointing at this app.

Status callbacks return 204. They do not send a reply.

## Suggested layout

The repo has no application stack yet. Use Python, Flask, and the official
`twilio` package. Document real run commands in `README.md`.

```
backend/app.py                  # webhook, ping, status
backend/agent/loop.py           # one loop
backend/agent/intent.py         # LLM intent parse
backend/agent/reply.py          # grounded wording
backend/nessie/client.py        # reads only
backend/twilio_client.py        # send body or content template
.env.example
```

Language model is OpenAI. `OPENAI_API_KEY` is already in `.env`. Load that
variable and call the OpenAI API for intent parsing and for wording the
reply. Do not swap in another provider. Do not paste the key into source
or docs. If the variable is missing, stop. Do not fake answers with canned
balances.

## Done when

- Mom can send a WhatsApp message to `+17372583478` and receive a reply.
- "What is my balance?" answers with the checking balance from Nessie.
- "What bills do I have?" lists payee, amount, payment date, and status from
  Nessie, including Austin Energy when that bill exists.
- A missing fact is stated as unknown. No invented dollar amounts.
- "Pay Austin Energy" does not create a Nessie bill payment.
- Gibberish gets a re-ask, not a guessed action.
- An invalid Twilio signature gets 403.
- `.env` is not tracked by git.
- `README.md` states how to run the server, open the tunnel, and where the
  webhook is set.

## Do not

- Do not commit credentials or paste them into docs, logs, or tests.
- Do not seed or delete Nessie data.
- Do not build Sarah's phone, the scammer box, the date picker, or execute.
- Do not send a free-form WhatsApp message when there has been no inbound
  message in the session window. Use the content template for that ping.
