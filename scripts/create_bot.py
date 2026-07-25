"""Send the SILENT listener bot into a Google Meet.

Silence is structural: no agent_config_id at all, so the bot cannot speak.
bot_name + bot_message post the two-party-consent disclosure to meeting
chat (California). custom_attributes carries the rep identity and is
echoed on every event — the correlation key.

No live-transcription webhook for now: the transcript is fetched after
the call, so no tunnel is needed.

Usage:
    python scripts/create_bot.py https://meet.google.com/xxx-xxxx-xxx \
        [--rep rep_a] [--session call-001]

Reads MEETSTREAM_API_KEY from .env (header format: "Authorization: Token").
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API_URL = "https://api.meetstream.ai/api/v1/bots/create_bot"

# Mia Transcribe native block (provided 2026-07-25) — replaces the
# deepgram_streaming config from docs/setup.md.
RECORDING_CONFIG = {
    "transcript": {
        "provider": {
            "meetstream": {
                "language": "auto",
                "translate": False,
            }
        }
    }
}

BOT_NAME = "Amplify Sales Manager (recording)"
BOT_MESSAGE = (
    "This call is being recorded and transcribed to build the agent "
    "coaching report. California is a two-party consent state - if anyone "
    "prefers not to be recorded, say so and I will leave."
)


def read_env(name):
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        raise SystemExit(f"missing {env_path} — cannot read {name}")
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            if value:
                return value
    raise SystemExit(f"{name} is empty or missing in .env")


def main():
    parser = argparse.ArgumentParser(description="Create the silent listener bot.")
    parser.add_argument("meeting_link", help="Google Meet link to join")
    parser.add_argument("--rep", default="rep_a", help="rep_identifier (default: rep_a)")
    parser.add_argument("--session", default="call-001", help="session tag (default: call-001)")
    args = parser.parse_args()

    api_key = read_env("MEETSTREAM_API_KEY")

    payload = {
        "meeting_link": args.meeting_link,
        "bot_name": BOT_NAME,
        "bot_message": BOT_MESSAGE,
        # deliberately NO agent_config_id — the silent bot must not speak
        "video_required": False,
        "recording_config": RECORDING_CONFIG,
        "custom_attributes": {"rep_identifier": args.rep, "session": args.session},
        "automatic_leave": {"waiting_room_timeout": 600, "everyone_left_timeout": 120},
    }

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            # MeetStream is "Token", not "Bearer" — the classic 401
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"create_bot failed: HTTP {e.code}\n{detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"create_bot failed: {e.reason}")

    print(json.dumps(body, indent=2))

    # Remember the bot for the post-call transcript fetch.
    last = REPO_ROOT / ".last_bot.json"
    last.write_text(json.dumps({
        "bot_id": body.get("bot_id") or body.get("id"),
        "rep_identifier": args.rep,
        "session": args.session,
        "meeting_link": args.meeting_link,
        "response": body,
    }, indent=2) + "\n")
    print(f"\nsaved bot details to {last}", file=sys.stderr)


if __name__ == "__main__":
    main()
