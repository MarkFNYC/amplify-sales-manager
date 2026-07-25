"""Fetch the finished MeetStream transcript and convert it to replay JSON.

Reads .last_bot.json (written by scripts/create_bot.py) for the bot_id /
transcript_id, pulls the transcript with the Token header, prints the RAW
response first, then converts it into the webhook event shape the
classifier consumes: speakerName, timestamp, new_text, end_of_turn,
word_is_final, custom_attributes.

Real response shape: an array of blocks, each with participant.name and
words[], where a word entry has text and start_timestamp.relative
(seconds as a float). Each block's words are joined into an utterance,
then split at sentence boundaries — every sentence becomes one committed
turn, timestamped from its own first word.

Usage:
    python scripts/fetch_transcript.py [--out transcripts/sample_call_003.json]
    python scripts/fetch_transcript.py --from-file raw_response.json   # offline
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API_BASE = "https://api.meetstream.ai"
DEFAULT_OUT = REPO_ROOT / "transcripts" / "sample_call_003.json"

SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")


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


def load_last_bot():
    path = REPO_ROOT / ".last_bot.json"
    if not path.exists():
        raise SystemExit(f"missing {path} — run scripts/create_bot.py first")
    data = json.loads(path.read_text())
    response = data.get("response") or {}
    return {
        "bot_id": data.get("bot_id") or response.get("bot_id") or response.get("id"),
        "transcript_id": data.get("transcript_id") or response.get("transcript_id"),
        "rep_identifier": data.get("rep_identifier", "rep_a"),
        "session": data.get("session", "call-003"),
    }


def fetch(api_key, bot):
    candidates = []
    if bot["transcript_id"]:
        candidates.append(f"{API_BASE}/api/v1/transcript/{bot['transcript_id']}/get_transcript")
    if bot["bot_id"]:
        candidates.append(f"{API_BASE}/api/v1/bots/{bot['bot_id']}/get_transcript")
        candidates.append(f"{API_BASE}/api/v1/bots/{bot['bot_id']}/transcript")
    if not candidates:
        raise SystemExit(".last_bot.json has neither transcript_id nor bot_id")

    errors = []
    for url in candidates:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Token {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                print(f"# fetched from {url}", file=sys.stderr)
                return raw
        except urllib.error.HTTPError as e:
            errors.append(f"{url} → HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
            if e.code != 404:
                break  # 401/403/500: retrying other paths won't help
        except urllib.error.URLError as e:
            raise SystemExit(f"fetch failed: {e.reason}")
    raise SystemExit("no endpoint returned a transcript:\n  " + "\n  ".join(errors))


def block_sentences(block, index):
    """Yield (speaker, t, text) — one per sentence in the block, each
    timestamped from its own first word."""
    try:
        speaker = block["participant"]["name"]
    except (KeyError, TypeError):
        raise SystemExit(f"block {index} has no participant.name — "
                         f"keys present: {sorted(block.keys())}")
    words = block.get("words") or []

    current = []
    for word in words:
        text = str(word.get("text", "")).strip()
        if not text:
            continue
        try:
            t = float(word["start_timestamp"]["relative"])
        except (KeyError, TypeError, ValueError):
            raise SystemExit(f"block {index} has a word without "
                             f"start_timestamp.relative: {word}")
        current.append((t, text))
        if SENTENCE_END.search(text):
            yield speaker, current[0][0], " ".join(w for _, w in current)
            current = []
    if current:  # trailing words with no terminal punctuation
        yield speaker, current[0][0], " ".join(w for _, w in current)


def convert(data, bot):
    blocks = data if isinstance(data, list) else None
    if blocks is None and isinstance(data, dict):
        for key in ("transcript", "transcripts", "segments", "data", "results"):
            if isinstance(data.get(key), list):
                blocks = data[key]
                break
    if blocks is None:
        raise SystemExit("could not find the array of blocks in the response — "
                         "see the raw dump above for the actual shape.")

    utterances = []
    for i, block in enumerate(blocks):
        utterances.extend(block_sentences(block, i))
    if not utterances:
        raise SystemExit("transcript contained no words — is the call finished?")

    custom = {"rep_identifier": bot["rep_identifier"], "session": bot["session"]}
    return [
        {
            "bot_id": bot["bot_id"] or "unknown",
            "speakerName": speaker,
            "timestamp": round(t, 2),
            "new_text": text,
            "transcript": text,
            "end_of_turn": True,
            "word_is_final": True,
            "custom_attributes": custom,
        }
        for speaker, t, text in sorted(utterances, key=lambda u: u[1])
    ]


def main():
    parser = argparse.ArgumentParser(description="Fetch and convert the finished transcript.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"output replay file (default: {DEFAULT_OUT})")
    parser.add_argument("--from-file", type=Path, default=None,
                        help="convert a saved raw response instead of fetching")
    args = parser.parse_args()

    bot = load_last_bot()

    if args.from_file:
        raw = args.from_file.read_text()
    else:
        raw = fetch(read_env("MEETSTREAM_API_KEY"), bot)

    # Raw response first, verbatim, so the real field names are visible.
    print("=== RAW RESPONSE " + "=" * 55)
    try:
        data = json.loads(raw)
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print(raw)
        raise SystemExit("response is not JSON — cannot convert")
    print("=" * 72)

    events = convert(data, bot)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(events, indent=2, ensure_ascii=False) + "\n")
    length = events[-1]["timestamp"] if events else 0
    m, s = divmod(int(length), 60)
    print(f"\nwrote {args.out} — {len(events)} events, {m}:{s:02d}")
    shown = args.out.relative_to(REPO_ROOT) if args.out.is_relative_to(REPO_ROOT) else args.out
    print(f"replay with: python src/classifier.py {shown}")


if __name__ == "__main__":
    main()
