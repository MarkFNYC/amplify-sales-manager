# Amplify Estates — AI Sales Manager · Setup

Environment: Scalekit workspace **Fabricacollective**, environment **Fabricacollective Dev**
Last verified: 2026-07-25

## 1. Scalekit — DONE

| Item | Value |
|---|---|
| Environment URL | `https://fabricacollective.scalekit.dev` |
| Environment ID | `env_118534958821148674` |
| Virtual MCP config name | `amplify-sales-manager` |
| **config_id** | `cfg_135792541596386050` |
| **mcp_server_url** | `https://fabricacollective.scalekit.dev/mcp/v3/servers/3a3f8a36-ce94-4427-98d2-fddc7ce423c1` |
| Gmail connection name | `gmail` (`conn_118534959391574018`) |
| Calendar connection name | `googlecalendar` (`conn_118551269949309703`) |
| User Verification mode | `Scalekit users only` (was `None`) |
| Identifiers | `rep_a`, `rep_b` |

Credential mode for both connections: **Use Scalekit credentials** (Scalekit's own OAuth app;
consent screen shows Scalekit branding). Fine for the demo.

### Tool allowlist — exactly 5, verified in the DOM before saving

googlecalendar (2 of 18):
- `googlecalendar_list_events` — Confirm Commitment check
- `googlecalendar_create_event` — book the follow-up

gmail (3 of 37):
- `gmail_fetch_mails` — prior-touch count
- `gmail_create_draft` — follow-up draft, saved not sent
- `gmail_get_send_as` — pulls the rep's real signature for Signature Voice

### Confirmed excluded
`gmail_send_message`, `gmail_send_draft`, `gmail_reply_to_thread`,
`gmail_delete_message`, `gmail_batch_delete_messages`, `gmail_delete_draft`,
`gmail_trash_message`, `gmail_delete_label`, `gmail_delete_filter`,
`googlecalendar_delete_event`, `googlecalendar_delete_calendar`,
`googlecalendar_delete_acl_rule`, `googlecalendar_insert_acl_rule`,
`googlecalendar_update_event`, `googlecalendar_move_event`.
The ACL tools matter: they would let the agent change who can see a calendar.

### Scopes verified on the connections
- `googlecalendar`: `/auth/calendar` (rw), `/auth/calendar.events` (rw),
  `.events.readonly`, `.readonly`, `.settings.readonly` — **write present, no redo needed**
- `gmail`: `gmail.readonly` (locked on), `gmail.modify` (rw — covers drafts),
  `gmail.send`, `contacts.readonly`, `contacts.other.readonly`

> **Open decision — `gmail.send` is granted.** The token can send; only the tool
> allowlist stops it. Unchecking it in AgentKit > Connections > gmail > Scopes is
> defense in depth, but the `gmail` connection is shared across this environment
> and every connected account must re-authorize after a scope change. Left as-is.

### Runtime sequence (per debrief session)
```python
from datetime import timedelta
from scalekit import ScalekitClient

client = ScalekitClient(env_url=ENV_URL, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
CFG = "cfg_135792541596386050"

# 1. every connection ACTIVE for this rep?
accts = client.actions.mcp.list_mcp_connected_accounts(
    config_id=CFG, identifier="rep_a", include_auth_link=True)
for a in accts.connected_accounts:
    if a.connected_account_status != "ACTIVE":
        raise SystemExit(f"{a.connection_name} needs auth: {a.authentication_link}")

# 2. fresh token, expiry must outlast the meeting (MIA does not refresh mid-session)
token = client.actions.mcp.create_session_token(
    mcp_config_id=CFG, identifier="rep_a", expiry=timedelta(hours=4)).token
```
The identifier **is** the identity. Never a service account. Same string everywhere.

## 2. MeetStream — NOT DONE, needs you

`app.meetstream.ai` is signed out. Do these four by hand:

1. Sign in / create the account at `app.meetstream.ai`.
2. API key at `app.meetstream.ai/api-keys`. Name it `amplify-sales-manager-hack`.
   Header format is `Authorization: Token <key>` — *not* Bearer.
3. Dashboard → Integrations: paste **Deepgram** and **OpenAI** keys. Nothing
   works without both; this is the number-one cause of "no voice, no transcript".
4. Create the debrief agent config with the call below, then capture
   `agent_config_id`.

### MIA agent config — one per rep (the session token lives in the header)
```bash
curl -X POST https://api.meetstream.ai/api/v1/mia \
  -H "Authorization: Token $MEETSTREAM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_name": "amplify-debrief-rep-a",
    "mode": "realtime",
    "model": {
      "provider": "openai",
      "model": "gpt-4o-realtime-preview",
      "voice": "sage",
      "temperature": 0.6,
      "system_prompt": "You are a sales manager debriefing a residential real estate agent after a prospect call, the way a manager talks in the car after a ride-along. Read the seven-stage coverage report aloud: covered stages briefly, missed stages with the timestamp and the verbatim quote as evidence. Frame every finding as a missed opportunity, never a mistake, never a grade. No scores. No tone or delivery critique. Never mention or infer familial status, race, religion, national origin, or disability, and never discuss neighborhoods in relation to who lives there. If the report says no follow-up meeting was booked, offer to book it and to draft the follow-up email, and only call a tool after the agent says yes out loud. Drafts are saved, never sent."
    },
    "agent": {
      "response_type": "voice",
      "first_message": "Ready when you are. Want the debrief?",
      "mcp_servers": [{
        "url": "https://fabricacollective.scalekit.dev/mcp/v3/servers/3a3f8a36-ce94-4427-98d2-fddc7ce423c1",
        "transport_type": "streamable_http",
        "headers": { "Authorization": "Bearer <SCALEKIT_SESSION_TOKEN_FOR_THIS_REP>" },
        "allowed_tools": [
          "googlecalendar_list_events",
          "googlecalendar_create_event",
          "gmail_fetch_mails",
          "gmail_create_draft",
          "gmail_get_send_as"
        ],
        "timeout": 30
      }]
    }
  }'
```
Save the agent **before** creating a bot. Each bot snapshots the config at creation.

## 3. create_bot payloads

### A. Silent listener — the prospect call
No `agent_config_id`, so silence is structural, not prompt-dependent.
```bash
curl -X POST https://api.meetstream.ai/api/v1/bots/create_bot \
  -H "Authorization: Token $MEETSTREAM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "meeting_link": "https://meet.google.com/xxx-xxxx-xxx",
    "bot_name": "Amplify Sales Manager (recording)",
    "bot_message": "This call is being recorded and transcribed to build the agent coaching report. California is a two-party consent state - if anyone prefers not to be recorded, say so and I will leave.",
    "video_required": false,
    "callback_url": "https://<tunnel>/meetstream/status",
    "live_transcription_required": { "webhook_url": "https://<tunnel>/meetstream/transcript" },
    "recording_config": {
      "transcript": { "provider": { "deepgram_streaming": {
        "model": "nova-2", "transcription_mode": "sentence", "language": "en",
        "punctuate": true, "smart_format": true, "endpointing": 300,
        "vad_events": true, "utterance_end_ms": 1000,
        "encoding": "linear16", "channels": 1
      }}}
    },
    "custom_attributes": { "rep_identifier": "rep_a", "session": "call-001" },
    "automatic_leave": { "waiting_room_timeout": 600, "everyone_left_timeout": 120 }
  }'
```
`custom_attributes` is echoed on every transcript event — that is the rep-identity
correlation key, so no guessing which agent a transcript belongs to.

Webhook payload fields: `bot_id`, `speakerName`, `timestamp`, `new_text`,
`transcript`, `words[]` (`start`/`end`/`confidence`), `end_of_turn`,
`word_is_final`, `custom_attributes`. Classify on `end_of_turn`, treat
`word_is_final: false` as interim, return 200 fast.

### B. Debrief bot — speaks
```bash
curl -X POST https://api.meetstream.ai/api/v1/bots/create_bot \
  -H "Authorization: Token $MEETSTREAM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "meeting_link": "https://meet.google.com/yyy-yyyy-yyy",
    "bot_name": "Amplify Sales Manager",
    "agent_config_id": "<AGENT_CONFIG_ID>",
    "video_required": false,
    "callback_url": "https://<tunnel>/meetstream/status",
    "custom_attributes": { "rep_identifier": "rep_a", "session": "call-001" }
  }'
```

## 4. Google Meet setup
Use Meet, not Zoom — Zoom needs your own app and host privileges. A human must
admit the bot from the lobby; pass `callback_url` and watch the status events so
you see `joining` / `in lobby` / `admitted` instead of guessing. Webhook must be
public https (ngrok or cloudflared) and return 2xx fast.

## 5. Two-user isolation demo
One config, one `mcp_server_url`, two identifiers. Mint a token for `rep_a`, run
the debrief, then mint for `rep_b` and run the same recorded call. Each sees only
its own mail and calendar. Finish on the negative case: ask rep_a's session for
something only in rep_b's mailbox and let it come back empty.

Audit trail: there is no per-tool-call audit log surface in AgentKit docs
(Logs shows activity, connected accounts show state). Log it yourself —
`{timestamp, rep_identifier, config_id, connection_name, tool_name, inputs_hash, outcome}`
— and render it next to the two reports.

## 6. .env (values live here only, never in this doc)
```dotenv
SCALEKIT_ENV_URL=https://fabricacollective.scalekit.dev
SCALEKIT_CLIENT_ID=skc_118534959290910722
SCALEKIT_CLIENT_SECRET=            # Settings > API Credentials, generate new secret
SCALEKIT_MCP_CONFIG_ID=cfg_135792541596386050
SCALEKIT_MCP_SERVER_URL=https://fabricacollective.scalekit.dev/mcp/v3/servers/3a3f8a36-ce94-4427-98d2-fddc7ce423c1
GMAIL_CONNECTION_NAME=gmail
CALENDAR_CONNECTION_NAME=googlecalendar
REP_A_IDENTIFIER=rep_a
REP_B_IDENTIFIER=rep_b

MEETSTREAM_API_KEY=                # app.meetstream.ai/api-keys
MEETSTREAM_AGENT_CONFIG_ID=        # from POST /api/v1/mia
DEEPGRAM_API_KEY=                  # also paste into MeetStream dashboard
OPENAI_API_KEY=                    # also paste into MeetStream dashboard
PUBLIC_WEBHOOK_BASE=               # ngrok https URL
```

## 7. Troubleshooting — things actually hit or waiting to bite

- **Duplicate connection names.** This environment has both `googlecalendar` and
  `googlecalendar-8pqjyQHv`. The Virtual MCP server and every `identifier` call
  must use plain `googlecalendar` / `gmail`. Wrong one = "tools appear but calls fail".
- **Tool picker is not searchable** and the list scrolls inside its own box.
  18 calendar tools sort oddly — `list_events` sits 14th, not with the other
  `list_*` tools. Verify the count reads `2 / 18` and `3 / 37` before saving.
- **Checkboxes are custom.** Clicking the row/label works; clicking the input
  does nothing. Confirm selection before hitting Create server.
- **Auth header mixup.** MeetStream = `Authorization: Token <api_key>`.
  Scalekit MCP = `Authorization: Bearer <session_token>`. This is the classic 401.
- **Token expiry.** MIA never refreshes mid-session. Expiry must outlast the meeting.
- **Stale bot config.** Save the agent, then create a *new* bot. Old bots keep the
  old snapshot and will not see the tools.
- **`Scalekit users only` verification** means whoever completes the consent must
  be invited to the Fabricacollective Scalekit workspace and logged in. If rep_b
  is a different Google identity, invite it first or the account never goes ACTIVE.
- **Consent tab auto-redirects.** Clicking Create Account immediately launches the
  hosted page and jumps straight to Google. Finish it in that tab; don't leave it
  parked or the state goes stale.
- **Free tier**: 5,000 tool calls/month, irrelevant today.

## 8. Still needs your hands

1. **Complete 4 OAuth consents.** The `gmail` + `rep_a` record is created but sitting
   on the Google consent screen and is **not** ACTIVE. Finish that one, then repeat
   via Connected Accounts > Add Account for: `gmail`+`rep_b`, `googlecalendar`+`rep_a`,
   `googlecalendar`+`rep_b`. All four must read ACTIVE.
   Use a *different* Google account for rep_b or the isolation demo proves nothing.
2. **All of section 2** — MeetStream sign-in, API key, Deepgram + OpenAI keys,
   MIA agent config.
3. **Decide on `gmail.send`** (section 1). Decision: leave as-is, note it as a
   production hardening item in the demo.
4. **Identifiers** stay `rep_a` / `rep_b`. No rewiring.