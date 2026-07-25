"""Seven-stage call classifier — file-replay path.

Reads a saved stream of MeetStream live-transcription webhook payloads
(fields: speakerName, timestamp, new_text, end_of_turn, word_is_final, ...),
reconstructs committed segments, classifies each into one of the seven
stages defined in stages.json, and detects the headline sequence violation:
provide_solution occurring before assess_need is complete.

No APIs, no keys, no network. Rule-based only.

Per stages.json constraints, the classifier's per-segment output is
constrained to a stage label plus a span reference (timestamp + quote).
It performs no inference beyond phrase matching — nothing here reads or
infers familial status, race, religion, national origin, or disability.

Usage:
    python src/classifier.py [transcripts/sample_call_001.json]
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGES_PATH = REPO_ROOT / "stages.json"
DEFAULT_TRANSCRIPT = REPO_ROOT / "transcripts" / "sample_call_001.json"

# Phrase cues per stage id. Ids must match stages.json — that file is
# authoritative for the stage list; this table only supplies the lexicon.
STAGE_CUES = {
    "engage": [
        r"how did you hear", r"\breferr(ed|al)\b", r"thanks for making the time",
        r"glad we could connect", r"congratulations", r"great to meet",
    ],
    "qualify": [
        r"driving the move", r"start date", r"\btimeline\b", r"need to be settled",
        r"\brenting\b", r"nothing to sell", r"home to sell", r"pre-?approved",
        r"\blender\b", r"\bcash\b", r"decide together", r"part of the process",
        r"who else is involved",
    ],
    "assess_need": [
        r"hoping for", r"must-?haves?", r"nice-?to-?have", r"what (you|we) (actually )?need",
        r"walk me through", r"what i'?m hearing", r"did i get that right",
        r"sounds like you", r"the big thing", r"\bcommute\b", r"wants, not needs",
    ],
    "capabilities_trust": [
        r"track record", r"my process", r"what happens next", r"buyer representation",
        r"\bcompensation\b", r"\bcommission\b", r"i'?ve helped", r"clients like you",
        r"how i work",
    ],
    "provide_solution": [
        r"perfect place", r"a listing on", r"i'?d recommend", r"recommend we",
        r"focus the search", r"filtered search", r"get us in", r"just dropped to",
        r"i can show you",
    ],
    "confirm_commitment": [
        r"lock in", r"\bschedule\b", r"book (a|the)", r"next meeting",
        r"(that|ten|saturday) works", r"can we (do|meet|lock)", r"first tour",
    ],
    "serve": [
        r"i'?ll send", r"check in every", r"follow up (with|by)", r"by tomorrow",
        r"you always know", r"keep you posted",
    ],
}

# assess_need completion, per its checks in stages.json: the need is
# reflected back, then a different speaker gives an explicit yes.
REFLECT_BACK = re.compile(
    r"what i'?m hearing|did i get that right|so to confirm|"
    r"just to make sure i have (this|that) right|sounds like you need",
    re.I,
)
EXPLICIT_YES = re.compile(
    r"^\s*(yes|yeah|yep|exactly|that'?s right|correct)\b", re.I,
)


def mmss(seconds):
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def load_events(path):
    with open(path) as f:
        data = json.load(f)
    events = data["events"] if isinstance(data, dict) else data
    return sorted(events, key=lambda e: e["timestamp"])


def committed_segments(events):
    """Rebuild committed turns: append final fragments, commit on end_of_turn.

    word_is_final: false events are interim and dropped — a final version of
    the same words always follows.
    """
    segments = []
    buf_text, buf_speaker, buf_start = "", None, None

    def flush():
        nonlocal buf_text, buf_speaker, buf_start
        if buf_text:
            segments.append({"speaker": buf_speaker, "t": buf_start, "text": buf_text.strip()})
        buf_text, buf_speaker, buf_start = "", None, None

    for ev in events:
        if not ev.get("word_is_final", True):
            continue
        if buf_speaker is not None and ev["speakerName"] != buf_speaker:
            flush()  # defensive: speaker changed without an end_of_turn
        if buf_speaker is None:
            buf_speaker, buf_start = ev["speakerName"], ev["timestamp"]
        buf_text = (buf_text + " " + ev["new_text"]).strip()
        if ev.get("end_of_turn"):
            flush()
    flush()
    return segments


def classify(segments, stage_ids):
    """Assign a stage id to each segment: highest cue-match count wins,
    ties break toward the earlier stage in canonical order, and segments
    with no matches inherit the previous segment's stage (continuations
    like a bare 'Yes, exactly right.')."""
    compiled = {
        sid: [re.compile(p, re.I) for p in STAGE_CUES.get(sid, [])]
        for sid in stage_ids
    }
    prev = None
    for seg in segments:
        scores = {
            sid: sum(1 for rx in pats if rx.search(seg["text"]))
            for sid, pats in compiled.items()
        }
        best = max(stage_ids, key=lambda sid: scores[sid])
        seg["stage"] = best if scores[best] > 0 else prev
        prev = seg["stage"]
    return segments


def assess_completed_at(segments):
    """Timestamp at which assess_need became complete: a reflect-back is
    answered with an explicit yes by a different speaker. None if never."""
    pending = None
    for seg in segments:
        if REFLECT_BACK.search(seg["text"]):
            pending = seg
        elif pending and seg["speaker"] != pending["speaker"] and EXPLICIT_YES.match(seg["text"]):
            return seg
    return None


def build_findings(segments, stages, sequence_rule):
    findings = []

    confirm_seg = assess_completed_at(segments)
    confirmed_t = confirm_seg["t"] if confirm_seg else None

    # Headline: provide_solution before assess_need is complete.
    for seg in segments:
        if seg["stage"] != "provide_solution":
            continue
        if confirmed_t is None or seg["t"] < confirmed_t:
            headline = sequence_rule["output"].replace("{timestamp}", mmss(seg["t"]))
            note = (
                f'The need was only confirmed later, at {mmss(confirmed_t)}: '
                f'"{confirm_seg["text"]}"'
                if confirmed_t is not None
                else "The need was never explicitly confirmed on this call."
            )
            findings.append({
                "kind": "SEQUENCE", "t": seg["t"], "headline": headline,
                "quote": seg, "note": note,
            })

    # Coverage gaps: stages with no segments at all. Evidence is the moment
    # the conversation first moved past the stage in canonical order.
    order = {s["id"]: i for i, s in enumerate(stages)}
    names = {s["id"]: s["name"] for s in stages}
    covered = {seg["stage"] for seg in segments}
    for stage in stages:
        sid = stage["id"]
        if sid in covered:
            continue
        later = next((s for s in segments if s["stage"] and order[s["stage"]] > order[sid]), None)
        findings.append({
            "kind": "COVERAGE", "t": later["t"] if later else None,
            "headline": f'"{names[sid]}" never came up — a missed opportunity.',
            "quote": later,
            "note": (
                f'The conversation reached "{names[later["stage"]]}" at '
                f"{mmss(later['t'])} without it."
                if later else "The call ended before this stage."
            ),
        })

    findings.sort(key=lambda f: (f["t"] is None, f["t"] or 0))
    return findings


def render_report(path, events, segments, stages, findings):
    names = {s["id"]: s["name"] for s in stages}
    attrs = next((e.get("custom_attributes") for e in events if e.get("custom_attributes")), {}) or {}
    lines = []
    w = lines.append

    w("=" * 72)
    w("AMPLIFY SALES MANAGER — CALL DEBRIEF (file replay)")
    w(f"  transcript : {path}")
    w(f"  rep        : {attrs.get('rep_identifier', 'unknown')}"
      f"    session: {attrs.get('session', 'unknown')}")
    w(f"  segments   : {len(segments)} committed"
      f"    length: {mmss(segments[-1]['t'])}" if segments else "  segments   : 0")
    w("=" * 72)

    w("")
    w("STAGE COVERAGE")
    for i, stage in enumerate(stages, 1):
        segs = [s for s in segments if s["stage"] == stage["id"]]
        if segs:
            span = f"{mmss(segs[0]['t'])}–{mmss(segs[-1]['t'])}"
            w(f"  {i}. {stage['name']:<42} covered   {len(segs):>2} segments   {span}")
        else:
            w(f"  {i}. {stage['name']:<42} MISSED")

    w("")
    w("SEGMENTS (stage label + span reference)")
    for seg in segments:
        label = names.get(seg["stage"], "unclassified")
        w(f"  {mmss(seg['t']):>5}  {seg['speaker']:<14} {label}")

    w("")
    w(f"FINDINGS — {len(findings)} missed opportunit"
      f"{'y' if len(findings) == 1 else 'ies'} (each with timestamp + verbatim quote)")
    if not findings:
        w("  Every stage was covered, in order. Nothing missed on this one.")
    for f in findings:
        w("")
        w(f"  [{f['kind']}] {f['headline']}")
        if f["quote"]:
            w(f'      {mmss(f["quote"]["t"])}  {f["quote"]["speaker"]}: "{f["quote"]["text"]}"')
        w(f"      {f['note']}")
    w("")
    return "\n".join(lines)


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TRANSCRIPT
    with open(STAGES_PATH) as f:
        config = json.load(f)
    stages = config["stages"]

    events = load_events(path)
    segments = committed_segments(events)
    classify(segments, [s["id"] for s in stages])
    findings = build_findings(segments, stages, config["sequence_rule"])
    print(render_report(path, events, segments, stages, findings))


if __name__ == "__main__":
    main()
