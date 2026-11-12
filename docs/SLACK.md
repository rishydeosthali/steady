# Slack demo

The hosted simulation can use Slack DMs instead of WhatsApp. Set
`STEADY_CHANNEL=slack`, `STEADY_SIMULATION=1`, `SLACK_BOT_TOKEN`,
`SLACK_SIGNING_SECRET`, `SLACK_TEAM_ID`, and `SLACK_SENIOR_USER_ID` in Render.
Keep the existing OpenAI and simulation directory settings. Twilio credentials
are unused in Slack mode; bill-photo attachments are not supported yet.

Install the bot with `chat:write`, `im:history`, and `im:write`. Enable App Home
messages. Subscribe to `message.im` at the HTTPS `/slack/events` endpoint after
deploying. Slack verifies the URL with a signed challenge. Incoming requests
must have a fresh valid Slack signature and the configured workspace/user.
Bot messages, edits, channel messages, and repeated event IDs are ignored.
Event IDs persist in the simulation directory. Processing is asynchronous;
an acknowledged event interrupted by a process crash may need a new user message.

By default caregiver approvals continue through the configured email provider.
To move approvals to Slack, set `SLACK_CAREGIVER_USER_ID` to a different member
and enable Interactivity at `/slack/actions`. Only that member can approve or
deny using the buttons; the senior cannot approve their own payment. The same
payee/amount/date binding, stale-consent checks, and simulated settlement apply.
Slack does not use WhatsApp's 24-hour messaging window.

Keep one hosted instance. Render Free can sleep; open the dashboard before the
demo. Its ephemeral storage can reset the simulation on restart. Switching
channels should use a fresh simulation directory to avoid mixing old approvals
and notifications. Do not put credentials in Git or chat.
