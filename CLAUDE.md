# Amplify Estates — AI Sales Manager (hackathon build)

## Mission
An AI sales manager that rides along on a realtor's prospect call, stays
silent, then debriefs them afterwards on what they missed. Part of
amplifyestates.com, an AI platform for residential real estate agents.
The platform's gap: every existing module improves the agent's output and
assumes competence in the live conversation, which is the weakest link.
Brokerages provide no sales management because agents are independent
contractors. This is the manager that was never there.

## Deliverable
Working demo submitted 3:45pm today. Judging 4:00 to 6:00. Team max 2.

## ARCHITECTURE — decided, do not relitigate

Two separate bots.
1. SILENT BOT for the prospect call. Created with
   live_transcription_required.webhook_url and NO agent config at all.
   Silence is structural, not prompt-dependent. Avoids echo and wake-word bugs.
2. DEBRIEF BOT for afterwards. Saved MIA agent config, realtime mode,
   agent.response_type "voice", Scalekit Virtual MCP attached under
   agent.mcp_servers[].

CRITICAL: the classifier MUST consume a saved transcript JSON file as its
primary path. Live webhook is the bonus path. Conference wifi at 4pm is a
real risk and file replay makes the demo unkillable. Build file-first.

Transcript webhook: commit segments on end_of_turn, treat word_is_final:false
as interim, ACK 2xx fast. Pass custom_attributes at bot creation
({"rep_identifier": "...", "prospect": "..."}) — echoed on every event, this
is the correlation key for rep identity.

Traps: MIA does not refresh tokens mid-session, so session token expiry must
outlast the meeting. Save the agent config BEFORE creating the bot.

## THE SEVEN STAGES
Defined in stages.json. That file is authoritative — do not invent stages.
1. Engage and build the relationship
2. Qualify the prospect
3. Assess and confirm need
4. Present capabilities and confirm trust
5. Provide solution
6. Confirm commitment
7. Serve customer

Mechanic is COVERAGE GAPS and SEQUENCE ERRORS. Not sentiment, not tone,
not delivery, no scores.

Headline detection: Provide Solution occurring before Assess & Confirm Need
is complete. This single rule is the demo. Output shape:
"You presented a solution at 4:12, before you confirmed what she needed."

Every finding carries a timestamp and a verbatim quote as evidence.
Classifier output is constrained to stage label plus span reference only.

## SCALEKIT TOOLS — exactly these, no others
READS
  googlecalendar_list_events  → was a next meeting actually booked?
                                objective check for Confirm Commitment
  gmail_fetch_mails           → count prior emailed touches
  gmail_get_send_as           → rep's real signature, pairs with Signature Voice
WRITES (gated on a BUTTON in the report UI, not voice confirmation)
  googlecalendar_create_event → book the follow-up the rep failed to book
  gmail_create_draft          → draft only, never send

EXCLUDED AT CONFIG LEVEL: gmail_send_message, gmail_send_draft, all deletes.
Least privilege at the tool level makes a hallucinated send impossible
rather than merely unlikely.

DO NOT use MeetStream's own calendar endpoints. They are MeetStream-level,
not per-rep scoped, and using them invalidates the entire claim the demo
exists to prove.

Runtime order, three calls per session:
  list_mcp_connected_accounts(config_id, identifier, include_auth_link=True)
    → confirm ACTIVE, surface authentication_link if not
  create_session_token(mcp_config_id, identifier, expiry=4h)
  pass {"Authorization": f"Bearer {token}"} as the MCP header
The identifier IS the identity. Same string everywhere, must match the
connected account. Config values live in docs/setup.md and .env.

## AUDIT LOG — build it yourself
No per-tool-call audit surface exists in the vendor docs. Log every call:
{timestamp, rep_identifier, config_id, connection_name, tool_name,
 inputs_hash, outcome}
Render as a small table beside the reports. Demos better than a vendor
dashboard because it sits on one screen with the two reports.

## ISOLATION DEMO — the differentiated claim
One config, one mcp_server_url, two identifiers, two authorized accounts,
two session tokens. Run the same recorded call twice. Rep A reads and writes
only into A's Gmail and Calendar, Rep B only into B's.
Then the negative case, which is what judges remember: ask Rep A's session
for something that exists only in B's mailbox and show it come back empty.

## DECIDED QUESTIONS
- Debrief speaks a PRE-COMPUTED script, not live reasoning. Reason: you
  cannot post-filter live reasoning, so live reasoning would eliminate the
  Fair Housing suppression layer. Live Q&A only after the script, only if ahead.
- Touch denominator is hardcoded at 5 (a pharma benchmark, not a real estate
  law). Label it "2 emailed touches of a 5-touch benchmark", never
  "touch 2 of 5". Gmail gives the numerator only, and calls, texts, DMs and
  open house meetings are not in a mailbox. Precision protects us if a judge
  is a realtor.
- gmail.send scope is granted on the shared connection. The tool allowlist is
  what prevents sending. Known production hardening item, stated openly.

## HARD CONSTRAINTS
Two-party consent (California): bot_name "Amplify Sales Manager · recording"
plus bot_message on create_bot, which posts disclosure to meeting chat
without the bot speaking.

Fair Housing, suppressed at THREE layers:
  1. Ingestion — redact before persisting
  2. Classifier — constrained output, explicit refusal to infer protected
     characteristics (familial status, race, religion, national origin,
     disability)
  3. Draft generator (riskiest) — content restricted to seven-stage gaps
     only, never neighborhood-versus-household reasoning. Post-generation
     term filter before anything reaches gmail_create_draft.
Steering risk is real. This is not optional polish.

MLS data is licensed per individual agent. Nothing crosses between agents.
Coaching records belong to the rep. Everything keyed by rep_identifier.
No broker read path exists in this build.

Tone: findings are MISSED OPPORTUNITIES, never mistakes or grades. Reps
abandon tools that feel like surveillance. Frame the calendar write as
"here's the follow-up you didn't get to book."

## SCHEDULE — hard times
12:15  Bot in a Meet, transcript arriving
1:15   Seven-stage classifier + sequence violation + report (same artifact)
1:45   RECORD THE MOCK CALL AND BACKUP VIDEO. Non-negotiable, does not slip
2:30   Scalekit reads plus the calendar write
3:00   Rep B authorized, isolation demo working
3:30   Voice debrief if alive, otherwise rehearse twice

## CUT ORDER
NEVER CUT: seven-stage report with evidence quotes. Pre-recorded backup video.
Cut in this order: voice debrief, then Gmail touch count, then live webhook
path (fall back to file replay), then the isolation demo.
Cutting the voice debrief forfeits the MeetStream credits prize since voice
is their differentiator. It goes first anyway because the isolation demo is
config rather than new code, and serves both the main criteria and the
Scalekit prize.

Skip Zoom entirely. Needs your own app and host privileges.

## FIRST TASK
Read stages.json and docs/setup.md. Build the file-replay path only, no APIs,
no network. Create a sample transcript JSON in transcripts/ matching the
MeetStream webhook shape, then src/classifier.py which reads it, classifies
segments into the seven stages, and detects the sequence violation. Output a
report with per-stage coverage and every finding carrying a timestamp and
verbatim quote. Nothing else until that works.